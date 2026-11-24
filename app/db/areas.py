"""Area reads: the parts of a plan a person drew around, stored.

Three things happen to an area read's boxes, and all three are here:

  saved      only the boxes answering the drawing are replaced; what they
             replaced is kept whole on the area, for undo
  kept       a read of the whole set does not wipe them -- see `carry_over`,
             which `store.save_audit` calls on either side of its rebuild
  undone     the area's own boxes come off and what they replaced goes back,
             except where something newer now stands

Best-effort, like the rest of the store: a failure here costs the memory of a
read, not the read.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.core.read_area import AreaPlan, answers
from app.db import store
from app.db.client import NoDatabase, client
from app.schemas import PlanAudit

log = logging.getLogger(__name__)

# The columns a stored detection carries that are not its content: made anew
# on every insert.
_IDENTITY = ("id", "sheet_id", "created_at")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _box(row: dict) -> tuple[float, float, float, float]:
    return (row.get("x0") or 0.0, row.get("y0") or 0.0,
            row.get("x1") or 0.0, row.get("y1") or 0.0)


# --------------------------------------------------------------------------- #
# the record
# --------------------------------------------------------------------------- #

def create(document_id: str, drawn: list[dict], *, created_by: str | None,
           job_id: str | None) -> dict[str, Any]:
    row = {"document_id": document_id, "drawn": drawn, "status": "queued",
           "created_by": created_by, "job_id": job_id}
    return client().table("area_reads").insert(row).execute().data[0]


def update(area_id: str, **fields) -> None:
    try:
        client().table("area_reads").update({**fields, "updated_at": _now()}).eq(
            "id", area_id).execute()
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        log.warning("db: area read %s not updated: %s", area_id, exc)


def get(area_id: str) -> dict[str, Any] | None:
    found = client().table("area_reads").select("*").eq("id", area_id).execute().data
    return found[0] if found else None


def for_document(document_id: str) -> list[dict[str, Any]]:
    return (client().table("area_reads").select("*")
            .eq("document_id", document_id)
            .order("created_at", desc=True).execute().data)


def _plans(area: dict) -> list[AreaPlan]:
    return [AreaPlan.from_json(p) for p in area.get("plan") or []]


def _active(db, document_id: str) -> list[dict]:
    """Area reads on this document whose boxes stand: read, not undone."""
    try:
        return (db.table("area_reads").select("*")
                .eq("document_id", document_id).eq("status", "ready")
                .execute().data)
    except Exception as exc:  # noqa: BLE001 - before db/037, there are none
        log.info("db: no area reads read (%s)", exc)
        return []


# --------------------------------------------------------------------------- #
# saved
# --------------------------------------------------------------------------- #

def save(document_id: str, area_id: str, audit: PlanAudit,
         plans: list[AreaPlan]) -> dict[str, Any] | None:
    """Store what an area read found, replacing only what answers the drawing.

    Replaced: every detected box on those parts that answers the drawing, and
    any other box of the same door on the same sheet (one row per door per
    sheet). A box a person placed or removed by hand stands as it is.
    """
    try:
        db = client()
        document = (db.table("documents").select("id, org_id, project_id")
                    .eq("id", document_id).execute().data)
        if not document:
            return None
        org_id = document[0]["org_id"]

        existing = (db.table("sheets").select("*")
                    .eq("document_id", document_id).execute().data)
        sheet_id = {s["page"]: s["id"] for s in existing}
        new = [s for s in audit.floor_plans if s.page not in sheet_id]
        made = store._insert("sheets", [
            {"document_id": document_id, "page": s.page, "number": s.number,
             "title": s.title, "level": s.level, "leads": s.leads,
             "scanned": s.scanned, "is_enlargement": s.is_enlargement,
             "width_pt": s.width, "height_pt": s.height}
            for s in new
        ])
        sheet_id.update({s.page: m["id"] for s, m in zip(new, made)})
        page_of = {v: k for k, v in sheet_id.items()}

        dead = store.tombstones(document_id)
        hand = {(m["page"], m["door_tag"]) for m in store.manual_detections(document_id)
                if m.get("door_tag")}
        rows, seen = [], set()
        for door in audit.detected:
            page = door.location.page
            if page not in sheet_id:
                continue
            if dead and store.is_suppressed(
                    {"page": page, "door_tag": door.tag or None,
                     "x0": door.location.x0, "y0": door.location.y0,
                     "x1": door.location.x1, "y1": door.location.y1}, dead):
                continue
            if door.tag and (page, door.tag) in hand:
                continue
            key = (door.tag or "", page)
            if door.tag and key in seen:
                continue
            seen.add(key)
            row = store._detection_row(org_id, document_id, sheet_id[page], door)
            row["area_id"] = area_id
            rows.append(row)

        current = (db.table("detections").select("*")
                   .eq("document_id", document_id).execute().data)
        new_tags = {(r["sheet_id"], r["door_tag"]) for r in rows if r["door_tag"]}
        replaced = [d for d in current
                    if answers(page_of.get(d["sheet_id"], -1), _box(d), plans)
                    or (d.get("door_tag")
                        and (d["sheet_id"], d["door_tag"]) in new_tags)]
        gone = {d["id"] for d in replaced}
        for start in range(0, len(replaced), store._BATCH):
            db.table("detections").delete().in_(
                "id", [d["id"] for d in replaced[start:start + store._BATCH]]).execute()

        # A door now counted on the sheet it was drawn on is no longer counted
        # where it was left, or it is counted twice.
        primary = {r["door_tag"] for r in rows if r["is_primary"] and r["door_tag"]}
        demote = [d["id"] for d in current if d["id"] not in gone
                  and d.get("is_primary") and d.get("door_tag") in primary]
        if demote:
            db.table("detections").update({"is_primary": False}).in_(
                "id", demote).execute()

        # A type a person set on a box this read replaces carries onto the
        # same door in what replaces it.
        store._keep_types(rows, [_typed(d, page_of) for d in replaced
                                 if d.get("kind_by_hand")], page_of)
        store._insert("detections", rows)
        written = {r["door_tag"] for r in rows if r["door_tag"]}
        store._record_walls(db, document_id,
                            [d for d in audit.detected if d.tag in written])

        snapshot = [{**{k: v for k, v in d.items() if k not in _IDENTITY},
                     "page": page_of.get(d["sheet_id"])} for d in replaced]
        update(area_id, replaced=snapshot, found=len(rows))
        log.info("db: area read %s replaced %d box(es) with %d",
                 area_id, len(replaced), len(rows))
        return {"replaced": len(replaced), "added": len(rows)}
    except NoDatabase:
        return None
    except Exception as exc:  # noqa: BLE001 - storing must not break a read
        log.warning("db: could not store area read %s: %s", area_id, exc)
        return None


def _typed(det: dict, page_of: dict[str, int]) -> dict:
    """A box whose type a person set, in the shape `_keep_types` reads."""
    return {"page": page_of.get(det["sheet_id"]), "door_tag": det.get("door_tag"),
            "kind": det.get("kind"), "swing": det.get("swing"),
            "cx": ((det.get("x0") or 0) + (det.get("x1") or 0)) / 2,
            "cy": ((det.get("y0") or 0) + (det.get("y1") or 0)) / 2}


# --------------------------------------------------------------------------- #
# kept through a read of the whole set
# --------------------------------------------------------------------------- #

class CarryOver:
    """What an area read placed, lifted out before a whole-set rebuild and put
    back after it -- so a person's paid read is not wiped by the next pass."""

    def __init__(self, rows: list[dict], plans: list[AreaPlan],
                 sheets: dict[int, dict]):
        self.rows = rows            # stored detections, each with its page
        self.plans = plans          # every active area's parts
        self.sheets = sheets        # page -> old sheet row, for pages to re-make

    def __bool__(self) -> bool:
        return bool(self.rows)

    def keeps(self, page: int, row: dict) -> bool:
        """Does an area read own this place? Then the rebuild leaves it be."""
        if any(r["page"] == page and r.get("door_tag")
               and r["door_tag"] == row.get("door_tag") for r in self.rows):
            return True
        return answers(page, _box(row), self.plans)


def lift(db, document_id: str) -> CarryOver:
    """Before a rebuild: the boxes active area reads placed, and where."""
    areas = _active(db, document_id)
    if not areas:
        return CarryOver([], [], {})
    try:
        ids = [a["id"] for a in areas]
        rows = (db.table("detections").select("*")
                .eq("document_id", document_id).in_("area_id", ids)
                .execute().data)
        sheets = {s["page"]: s for s in
                  db.table("sheets").select("*")
                  .eq("document_id", document_id).execute().data}
    except Exception as exc:  # noqa: BLE001 - before db/037
        log.info("db: area boxes not carried over (%s)", exc)
        return CarryOver([], [], {})
    by_id = {s["id"]: p for p, s in sheets.items()}
    for r in rows:
        r["page"] = by_id.get(r["sheet_id"])
    rows = [r for r in rows if r["page"] is not None]
    plans = [p for a in areas for p in _plans(a)]
    return CarryOver(rows, plans, sheets)


def put_back(db, document_id: str, carry: CarryOver, new_rows: list[dict],
             sheet_id: dict[int, str]) -> list[dict]:
    """After a rebuild's sheets are made and before its boxes are written:
    drop the rebuilt boxes where an area read stands, re-make any sheet the
    rebuild no longer lists, and return the rows to write.
    """
    if not carry:
        return new_rows
    page_of = {v: k for k, v in sheet_id.items()}
    missing = sorted({r["page"] for r in carry.rows} - set(sheet_id))
    if missing:
        made = store._insert("sheets", [
            {k: v for k, v in carry.sheets[p].items() if k not in ("id", "created_at")}
            for p in missing if p in carry.sheets
        ])
        sheet_id.update({m["page"]: m["id"] for m in made})
    kept_rows = [r for r in new_rows
                 if not carry.keeps(page_of.get(r["sheet_id"], -1), r)]
    primary = {r["door_tag"] for r in carry.rows if r.get("is_primary") and r.get("door_tag")}
    for r in kept_rows:
        if r.get("door_tag") in primary:
            r["is_primary"] = False
    back = []
    for r in carry.rows:
        if r["page"] not in sheet_id:
            continue
        row = {k: v for k, v in r.items() if k not in ("sheet_id", "page", "created_at")}
        row["sheet_id"] = sheet_id[r["page"]]
        back.append(row)
    log.info("db: %d box(es) from area reads kept through the rebuild; %d "
             "rebuilt box(es) left out where they stand",
             len(back), len(new_rows) - len(kept_rows))
    return _same_keys(kept_rows + back)


def _same_keys(rows: list[dict]) -> list[dict]:
    """One shape for every row of a bulk insert."""
    keys = set().union(*(r.keys() for r in rows)) if rows else set()
    for r in rows:
        for k in keys:
            r.setdefault(k, None)
    return rows


# --------------------------------------------------------------------------- #
# undone
# --------------------------------------------------------------------------- #

def undo(area_id: str) -> dict[str, Any] | None:
    """Take an area read's boxes off and put back what they replaced.

    A replaced box goes back only where nothing newer stands: not where the
    same door is now boxed on that sheet, nor where any box now covers its
    spot, nor where a person has since removed it.
    """
    db = client()
    area = get(area_id)
    if area is None:
        return None
    if area["status"] == "undone":
        return {"area_id": area_id, "status": "undone", "removed": 0, "restored": 0}
    document_id = area["document_id"]

    removed = (db.table("detections").delete().eq("area_id", area_id)
               .execute().data) if area["status"] == "ready" else []

    restored = 0
    snapshot = area.get("replaced") or []
    if snapshot:
        sheets = (db.table("sheets").select("id, page")
                  .eq("document_id", document_id).execute().data)
        sheet_id = {s["page"]: s["id"] for s in sheets}
        page_of = {v: k for k, v in sheet_id.items()}
        standing = (db.table("detections").select("*")
                    .eq("document_id", document_id).execute().data)
        taken_tags = {(page_of.get(d["sheet_id"]), d["door_tag"])
                      for d in standing if d.get("door_tag")}
        taken_boxes = [(page_of.get(d["sheet_id"]), _box(d)) for d in standing]
        dead = store.tombstones(document_id)
        hand = {(m["page"], m["door_tag"]) for m in store.manual_detections(document_id)
                if m.get("door_tag")}
        done_areas = {a["id"] for a in for_document(document_id) if a["status"] == "undone"}
        done_areas.add(area_id)

        back = []
        for r in snapshot:
            page = r.get("page")
            if page not in sheet_id:
                continue
            tag = r.get("door_tag")
            if tag and ((page, tag) in taken_tags or (page, tag) in hand):
                continue
            if not tag and any(p == page and _covers(b, _box(r)) for p, b in taken_boxes):
                continue
            if dead and store.is_suppressed({**r, "page": page}, dead):
                continue
            row = {k: v for k, v in r.items() if k != "page"}
            row["sheet_id"] = sheet_id[page]
            if row.get("area_id") in done_areas:
                row["area_id"] = None
            back.append(row)
        if back:
            store._insert("detections", _same_keys(back))
        restored = len(back)

    update(area_id, status="undone", undone_at=_now())
    log.info("db: area read %s undone: %d box(es) off, %d put back",
             area_id, len(removed), restored)
    return {"area_id": area_id, "status": "undone",
            "removed": len(removed), "restored": restored}


def _covers(a: tuple, b: tuple) -> bool:
    """Do two boxes overlap by enough to be the same door?"""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return smaller > 0 and ix * iy >= 0.5 * smaller
