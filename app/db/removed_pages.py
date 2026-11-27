"""Plan pages a person removed from a drawing set, and putting them back.

  removed    the page's boxes come off -- the ones the plan reads placed and
             the ones a person placed -- and are kept whole on the removal.
             No door and no schedule row changes: a door whose only box was on
             the page is left unplaced. A door whose counted box was there and
             that is drawn on another sheet too is counted on that sheet now.
  skipped    while a page is removed, plan reads and area reads do not look at
             it, and anything they find there is not stored (`leave_out`).
  restored   its boxes go back, and the door it counted is counted on it
             again -- unless a person has since placed that door elsewhere.

Needs db/039_removed_pages.sql. Before that has run, nothing is listed as
removed and a removal is refused (`available`).
"""

from __future__ import annotations

import logging
from typing import Any

from app.db import store
from app.db.client import NoDatabase, client
from app.schemas import PlanAudit

log = logging.getLogger(__name__)

TABLE = "removed_pages"
# The columns of a stored box that are not its content: made anew on insert.
_IDENTITY = ("id", "sheet_id", "created_at")


def _box(row: dict) -> tuple[float, float, float, float]:
    return (row.get("x0") or 0.0, row.get("y0") or 0.0,
            row.get("x1") or 0.0, row.get("y1") or 0.0)


def available() -> bool:
    """Has db/039 been run?"""
    try:
        client().table(TABLE).select("id").limit(1).execute()
        return True
    except Exception as exc:  # noqa: BLE001 - the table may not exist yet
        log.info("db: removed pages not available (has db/039 been run?): %s", exc)
        return False


def pages(document_id: str, db=None) -> set[int]:
    """The pages removed from this set. Empty before db/039."""
    try:
        rows = ((db or client()).table(TABLE).select("page")
                .eq("document_id", document_id).execute().data or [])
    except NoDatabase:
        return set()
    except Exception as exc:  # noqa: BLE001 - see the docstring
        log.info("db: removed pages not read (has db/039 been run?): %s", exc)
        return set()
    return {r["page"] for r in rows}


def listed(document_id: str) -> list[dict[str, Any]]:
    """The removed pages as a caller sees them, in page order: the sheet's
    number and title where known, when it was removed, and how many boxes
    putting it back would return."""
    try:
        rows = (client().table(TABLE).select("*")
                .eq("document_id", document_id).order("page").execute().data or [])
    except Exception as exc:  # noqa: BLE001 - before db/039 there are none
        log.info("db: removed pages not listed (%s)", exc)
        return []
    if not rows:
        return []
    named = {p["page"]: p for p in store.document_pages(document_id)}
    out = []
    for r in rows:
        sheet = r.get("sheet") or {}
        name = named.get(r["page"], {})
        out.append({"page": r["page"],
                    "number": sheet.get("number") or name.get("number"),
                    "title": sheet.get("title") or name.get("title"),
                    "removed_at": r.get("created_at"),
                    "removed_by": r.get("created_by"),
                    "boxes": len(r.get("detections") or []) + len(r.get("manual") or [])})
    return out


# --------------------------------------------------------------------------- #
# removed
# --------------------------------------------------------------------------- #

def _promotions(mine: list[dict], hand: list[dict], others: list[dict],
                hand_else: list[dict], sheets: list[dict]) -> list[dict]:
    """Doors counted on this page and drawn on another sheet too: the box to
    count each on now. The sheet that leads its storey first, then a whole
    plan before an enlargement, then page order."""
    sheet = {s["id"]: s for s in sheets}
    lost = ({d["door_tag"] for d in mine if d.get("door_tag") and d.get("is_primary")}
            | {m["door_tag"] for m in hand if m.get("door_tag")})
    counted = ({d["door_tag"] for d in others if d.get("door_tag") and d.get("is_primary")}
               | {m["door_tag"] for m in hand_else if m.get("door_tag")})

    def rank(d: dict):
        s = sheet.get(d["sheet_id"]) or {}
        return (not s.get("leads"), bool(s.get("is_enlargement")), s.get("page") or 0)

    out = []
    for tag in sorted(lost - counted):
        left = [d for d in others if d.get("door_tag") == tag]
        if left:
            out.append({"id": min(left, key=rank)["id"], "door_tag": tag})
    return out


def remove(document_id: str, page: int, *, created_by: str | None) -> dict[str, Any]:
    """Take a page's boxes off and keep them, so the page can be put back.
    Removing a page already removed changes nothing."""
    db = client()
    answer = {"document_id": document_id, "page": page, "already_removed": False,
              "boxes_removed": 0, "hand_placed_removed": 0, "doors_unplaced": [],
              "counted_elsewhere": []}
    if (db.table(TABLE).select("id").eq("document_id", document_id)
            .eq("page", page).execute().data):
        return {**answer, "already_removed": True}

    sheets = (db.table("sheets").select("*").eq("document_id", document_id)
              .execute().data or [])
    sheet = next((s for s in sheets if s["page"] == page), None)
    every = (db.table("detections").select("*").eq("document_id", document_id)
             .execute().data or [])
    on_page = (lambda d: sheet is not None and d["sheet_id"] == sheet["id"])
    mine = [d for d in every if on_page(d)]
    others = [d for d in every if not on_page(d)]
    placed = (db.table("manual_detections").select("*").eq("document_id", document_id)
              .execute().data or [])
    hand = [m for m in placed if m.get("page") == page]
    hand_else = [m for m in placed if m.get("page") != page]
    promoted = _promotions(mine, hand, others, hand_else, sheets)

    # Kept first: nothing comes off unless it can be put back.
    db.table(TABLE).insert({
        "document_id": document_id, "page": page,
        "sheet": sheet,
        "detections": [{**{k: v for k, v in d.items() if k not in _IDENTITY},
                        "page": page} for d in mine],
        "manual": [{k: v for k, v in m.items() if k != "id"} for m in hand],
        "promoted": promoted, "created_by": created_by,
    }).execute()

    for table, rows in (("detections", mine), ("manual_detections", hand)):
        ids = [r["id"] for r in rows]
        for start in range(0, len(ids), store._BATCH):
            db.table(table).delete().in_("id", ids[start:start + store._BATCH]).execute()
    if promoted:
        db.table("detections").update({"is_primary": True}).in_(
            "id", [p["id"] for p in promoted]).execute()

    still = ({d["door_tag"] for d in others if d.get("door_tag")}
             | {m["door_tag"] for m in hand_else if m.get("door_tag")})
    gone = ({d["door_tag"] for d in mine if d.get("door_tag")}
            | {m["door_tag"] for m in hand if m.get("door_tag")})
    log.info("db: page %d removed from %s: %d box(es), %d placed by hand, "
             "%d door(s) now counted on another sheet", page, document_id,
             len(mine), len(hand), len(promoted))
    return {**answer, "boxes_removed": len(mine), "hand_placed_removed": len(hand),
            "doors_unplaced": sorted(gone - still),
            "counted_elsewhere": sorted(p["door_tag"] for p in promoted)}


# --------------------------------------------------------------------------- #
# skipped
# --------------------------------------------------------------------------- #

def leave_out(db, document_id: str, audit: PlanAudit) -> PlanAudit:
    """A plan read's findings without the pages removed from the set -- for a
    read that was already running when a page was removed."""
    gone = pages(document_id, db)
    if not gone:
        return audit
    detected = [d for d in audit.detected if d.location.page not in gone]
    left = len(audit.detected) - len(detected)
    if left:
        log.info("db: %d box(es) on removed page(s) of %s not stored", left, document_id)
    return audit.model_copy(update={
        "detected": detected,
        "floor_plans": [s for s in audit.floor_plans if s.page not in gone],
        "unread": [u for u in audit.unread if u.page not in gone],
    })


def take_area_back(db, document_id: str, area_id: str, snapshot: list[dict]) -> set[int]:
    """An area read undone while a page it read is removed: on that page, its
    own boxes come off what the removal kept and the boxes it replaced take
    their place, so putting the page back puts back what was there before the
    read. Returns the removed pages, whose snapshot rows are dealt with here."""
    try:
        kept = (db.table(TABLE).select("id, page, detections")
                .eq("document_id", document_id).execute().data or [])
    except Exception as exc:  # noqa: BLE001 - before db/039 there are none
        log.info("db: removed pages not read for undo (%s)", exc)
        return set()
    for row in kept:
        boxes = row.get("detections") or []
        mine = [d for d in boxes if d.get("area_id") != area_id]
        back = [dict(r) for r in snapshot if r.get("page") == row["page"]]
        if len(mine) != len(boxes) or back:
            db.table(TABLE).update({"detections": mine + back}).eq(
                "id", row["id"]).execute()
    return {row["page"] for row in kept}


# --------------------------------------------------------------------------- #
# restored
# --------------------------------------------------------------------------- #

def _same_keys(rows: list[dict]) -> list[dict]:
    """One shape for every row of a bulk insert."""
    keys = set().union(*(r.keys() for r in rows)) if rows else set()
    for r in rows:
        for k in keys:
            r.setdefault(k, None)
    return rows


def _put_back(db, document_id: str, kept: dict) -> dict[str, Any]:
    page = kept["page"]
    sheets = (db.table("sheets").select("*").eq("document_id", document_id)
              .execute().data or [])
    sheet = next((s for s in sheets if s["page"] == page), None)
    boxes = kept.get("detections") or []
    if sheet is None and kept.get("sheet"):
        # A read of the whole set rebuilt the sheets without this page.
        sheet = db.table("sheets").insert(
            {k: v for k, v in kept["sheet"].items() if k not in ("id", "created_at")}
        ).execute().data[0]

    back: list[dict] = []
    demote: list[str] = []
    if sheet is not None and boxes:
        standing = (db.table("detections").select("*").eq("document_id", document_id)
                    .execute().data or [])
        here = [d for d in standing if d["sheet_id"] == sheet["id"]]
        here_tags = {d["door_tag"] for d in here if d.get("door_tag")}
        placed_else = {m["door_tag"] for m in
                       (db.table("manual_detections").select("door_tag, page")
                        .eq("document_id", document_id).execute().data or [])
                       if m.get("door_tag") and m.get("page") != page}
        promoted = {p["id"] for p in kept.get("promoted") or []}
        for d in boxes:
            tag = d.get("door_tag")
            # Something newer already stands there: it wins.
            if tag and tag in here_tags:
                continue
            if not tag and any(_covers(_box(h), _box(d)) for h in here):
                continue
            row = {k: v for k, v in d.items() if k not in _IDENTITY and k != "page"}
            row["sheet_id"] = sheet["id"]
            if tag and row.get("is_primary"):
                theirs = [s["id"] for s in standing if s.get("door_tag") == tag
                          and s.get("is_primary") and s["sheet_id"] != sheet["id"]]
                if tag in placed_else or (theirs and not set(theirs) <= promoted):
                    # Counted elsewhere by something other than the removal.
                    row["is_primary"] = False
                else:
                    demote += theirs
            back.append(row)
    if demote:
        db.table("detections").update({"is_primary": False}).in_("id", demote).execute()
    if back:
        store._insert("detections", _same_keys(back))

    manual = [{k: v for k, v in m.items() if k != "id"} for m in kept.get("manual") or []]
    if manual:
        store._insert("manual_detections", _same_keys(manual))

    db.table(TABLE).delete().eq("id", kept["id"]).execute()
    log.info("db: page %d of %s put back: %d box(es), %d placed by hand",
             page, document_id, len(back), len(manual))
    return {"page": page, "boxes_restored": len(back),
            "hand_placed_restored": len(manual)}


def restore(document_id: str, page: int | None = None) -> list[dict[str, Any]] | None:
    """Put one removed page back, or every one. None where that page is not
    removed."""
    db = client()
    query = db.table(TABLE).select("*").eq("document_id", document_id)
    if page is not None:
        query = query.eq("page", page)
    kept = query.execute().data or []
    if page is not None and not kept:
        return None
    return [_put_back(db, document_id, k) for k in sorted(kept, key=lambda k: k["page"])]


def _covers(a: tuple, b: tuple) -> bool:
    """Do two boxes overlap by enough to be the same door?"""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return smaller > 0 and ix * iy >= 0.5 * smaller
