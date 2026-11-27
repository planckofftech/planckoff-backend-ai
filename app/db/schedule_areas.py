"""Schedule area reads: the door schedule tables a person drew around, stored.

What a read does to the schedule:

  added      a door number the schedule did not have becomes a door, tied to
             this read (doors.schedule_area_id)
  updated    a door the schedule already had takes the values this read found
             for it -- only the ones it found; an empty cell never blanks a
             value. What it held before is kept on the read, for undo.
             A person's corrections are never touched: they live apart, keyed
             on the door number, and stand over whatever any read says.
  table      where the box covers a table the schedule already lists, the read
             takes that table over -- its title, headings, count and outline
             become the box's -- rather than listing the same printed table
             twice. What the table said before is kept, for undo. A table not
             listed yet is added.
  wanted     a table the person removed earlier and now draws around is wanted
             after all -- their newer act wins. It comes off the removed list
             (undo puts it back on).
  replaced   a read of the whole schedule replaces everything the system read,
             this included; only what a person did by hand stands. The read is
             marked superseded and its undo does nothing -- see `supersede`,
             called from `store.save_extraction`.
  undone     the doors it added go, the values it replaced come back, a table
             it took over says what it said before, a table it added comes off.

Doors are matched by number and level, as the schedule keeps them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.db import store
from app.db.client import NoDatabase, client
from app.schemas import DoorRow, ScheduleTable

log = logging.getLogger(__name__)

TABLE = "schedule_area_reads"
SUPERSEDED = "superseded"
_SUPERSEDED_NOTE = "Replaced by a later read of the whole schedule."
# Door fields a read may set. `extra` is merged, never replaced.
_FIELDS = tuple(f for f in DoorRow.model_fields if f not in ("extra", "door_tag", "level"))
# A listed table is the one the box read when this share of the smaller of
# the two outlines is covered by both.
_SAME_TABLE = 0.5
# What a table takeover changes, and so what undo puts back.
_TABLE_FIELDS = ("title", "headers", "field_map", "row_count", "box_x0", "box_y0",
                 "box_x1", "box_y1", "box_source", "schedule_area_id")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create(document_id: str, drawn: list[dict], *, created_by: str | None,
           job_id: str | None) -> dict[str, Any]:
    row = {"document_id": document_id, "drawn": drawn, "status": "queued",
           "created_by": created_by, "job_id": job_id}
    return client().table(TABLE).insert(row).execute().data[0]


def update(read_id: str, **fields) -> None:
    try:
        client().table(TABLE).update({**fields, "updated_at": _now()}).eq(
            "id", read_id).execute()
    except Exception as exc:  # noqa: BLE001 - losing the note must not lose the read
        log.warning("db: schedule area read %s not updated: %s", read_id, exc)


def get(read_id: str) -> dict[str, Any] | None:
    found = client().table(TABLE).select("*").eq("id", read_id).execute().data
    return found[0] if found else None


def for_document(document_id: str) -> list[dict[str, Any]]:
    return (client().table(TABLE).select("*").eq("document_id", document_id)
            .order("created_at", desc=True).execute().data)


# --------------------------------------------------------------------------- #
# applying a read to the schedule
# --------------------------------------------------------------------------- #

def _match(existing: list[dict], tag: str, level: str) -> dict | None:
    """The door this row is: same number and level; else the only door with
    that number, where one of the two names no level."""
    same = [d for d in existing if d.get("door_tag") == tag]
    exact = [d for d in same if (d.get("level") or "") == level]
    if exact:
        return exact[0]
    if len(same) == 1 and (not level or not (same[0].get("level") or "")):
        return same[0]
    return None


def _changes(door: dict, row: DoorRow) -> tuple[dict, dict]:
    """What this row changes on a stored door, and what those fields held."""
    values = row.model_dump()
    new, before = {}, {}
    for f in _FIELDS:
        v = values.get(f)
        if isinstance(v, str) and v.strip() and v != (door.get(f) or ""):
            new[f], before[f] = v, door.get(f)
    extra = dict(door.get("extra") or {})
    merged = {**extra, **{k: v for k, v in (row.extra or {}).items() if v}}
    if merged != extra:
        new["extra"], before["extra"] = merged, extra
    return new, before


def _outline(row: dict) -> tuple[float, float, float, float] | None:
    box = tuple(row.get(k) for k in ("box_x0", "box_y0", "box_x1", "box_y1"))
    return box if all(v is not None for v in box) else None


def _listed(listed: list[dict], table: ScheduleTable, taken: set[str]) -> dict | None:
    """The table the schedule already lists that this box read, if any: on
    the same page, its outline mostly inside the box read's or the other way
    round. A table without an outline cannot be told apart and is left be."""
    if table.box is None:
        return None
    a = (table.box.x0, table.box.y0, table.box.x1, table.box.y1)
    best, most = None, 0.0
    for row in listed:
        b = _outline(row)
        if row.get("page") != table.page or b is None or row["id"] in taken:
            continue
        ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
        iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
        smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
        share = ix * iy / smaller if smaller > 0 else 0.0
        if share >= _SAME_TABLE and share > most:
            best, most = row, share
    return best


def _table_values(t: ScheduleTable, read_id: str) -> dict:
    return {"title": t.title, "headers": t.headers, "field_map": t.field_map,
            "row_count": t.row_count,
            "box_x0": t.box.x0 if t.box else None, "box_y0": t.box.y0 if t.box else None,
            "box_x1": t.box.x1 if t.box else None, "box_y1": t.box.y1 if t.box else None,
            "box_source": t.box.source if t.box else None,
            "schedule_area_id": read_id}


def _apply(db, document_id: str, org_id: str, read_id: str,
           tables: list[ScheduleTable]) -> dict[str, Any]:
    """Write one read's tables into the schedule. Returns what it did, and
    what it replaced (for undo)."""
    # A table the person removed earlier and has now drawn around is wanted
    # after all: their newer act wins. It comes off the removed list, so the
    # next read of the whole schedule keeps it too; undo puts it back.
    removed = store.removed_schedules(db, document_id)
    wanted_back = [g for g in removed
                   if any(store._same_schedule(g, t.page, t.title) for t in tables)]
    if wanted_back:
        db.table("documents").update(
            {"removed_schedules": [g for g in removed if g not in wanted_back]}
        ).eq("id", document_id).execute()

    listed = (db.table("schedules").select("*").eq("document_id", document_id)
              .execute().data or [])
    taken: set[str] = set()
    took_over: list[dict] = []
    schedule_ids: list[str] = []
    for t in tables:
        mine = _listed(listed, t, taken)
        if mine is not None:
            taken.add(mine["id"])
            took_over.append({"id": mine["id"],
                              "before": {k: mine.get(k) for k in _TABLE_FIELDS}})
            db.table("schedules").update(_table_values(t, read_id)).eq(
                "id", mine["id"]).execute()
            schedule_ids.append(mine["id"])
        else:
            made = store._insert("schedules", [
                {"document_id": document_id, "page": t.page, **_table_values(t, read_id)}])
            schedule_ids.append(made[0]["id"])

    existing = (db.table("doors").select("*").eq("document_id", document_id)
                .execute().data or [])
    last = max((d.get("row_index") or 0 for d in existing), default=-1)
    added, updated, seen, untagged = [], [], set(), 0
    for table, schedule_id in zip(tables, schedule_ids):
        for row in table.rows:
            tag = (row.door_tag or "").strip()
            if not tag:
                untagged += 1
                continue
            level = row.level or ""
            if (tag, level) in seen:
                continue
            seen.add((tag, level))
            door = _match(existing, tag, level)
            if door is None:
                last += 1
                values = row.model_dump(exclude={"extra"})
                values["door_tag"] = tag
                added.append({"org_id": org_id, "document_id": document_id,
                              "schedule_id": schedule_id, "row_index": last,
                              "source": "schedule", "schedule_area_id": read_id,
                              **values, "extra": row.extra})
                continue
            new, before = _changes(door, row)
            if not new:
                continue
            db.table("doors").update(new).eq("id", door["id"]).execute()
            updated.append({"door_tag": tag, "level": door.get("level") or "",
                            "before": before})
    if added:
        store._insert_doors(added)
    return {"added": len(added), "updated": updated, "untagged": untagged,
            "tables": took_over, "unremoved": wanted_back,
            "added_tags": sorted({a["door_tag"] for a in added})}


def save(document_id: str, read_id: str, tables: list[ScheduleTable],
         method: str) -> dict[str, Any] | None:
    """Apply a fresh read and record it. None where it could not be stored."""
    try:
        db = client()
        document = (db.table("documents").select("org_id").eq("id", document_id)
                    .execute().data)
        if not document:
            return None
        done = _apply(db, document_id, document[0]["org_id"], read_id, tables)
        update(read_id, tables=[t.model_dump(mode="json") for t in tables],
               replaced={"doors": done["updated"], "tables": done["tables"],
                         "unremoved": done["unremoved"]},
               added=done["added"], updated=len(done["updated"]), method=method)
        log.info("db: schedule area read %s: %d door(s) added, %d updated, "
                 "%d table(s) taken over", read_id, done["added"],
                 len(done["updated"]), len(done["tables"]))
        return {"added": done["added"], "updated": len(done["updated"]),
                "untagged": done["untagged"], "added_tags": done["added_tags"]}
    except NoDatabase:
        return None
    except Exception as exc:  # noqa: BLE001 - the schedule stands as it was
        log.warning("db: could not store schedule area read %s: %s", read_id, exc)
        return None


# --------------------------------------------------------------------------- #
# replaced by a read of the whole schedule
# --------------------------------------------------------------------------- #

def supersede(document_id: str) -> int:
    """A read of the whole schedule has replaced every door and table: mark
    the box reads that stood as replaced. Their undo then does nothing -- it
    would put back values from before that read, on top of it."""
    try:
        done = (client().table(TABLE)
                .update({"status": SUPERSEDED, "message": _SUPERSEDED_NOTE,
                         "updated_at": _now()})
                .eq("document_id", document_id).eq("status", "ready")
                .execute().data or [])
    except Exception as exc:  # noqa: BLE001 - before db/038 there are none
        log.info("db: schedule area reads not marked replaced (%s)", exc)
        return 0
    if done:
        log.info("db: %d schedule area read(s) replaced by a read of the whole "
                 "schedule", len(done))
    return len(done)


# --------------------------------------------------------------------------- #
# undone
# --------------------------------------------------------------------------- #

def undo(read_id: str) -> dict[str, Any] | None:
    """Take a read off the schedule. Undoing twice is the same as once; a read
    a whole-schedule read has replaced has nothing left to undo."""
    db = client()
    read = get(read_id)
    if read is None:
        return None
    if read["status"] in ("undone", SUPERSEDED):
        return {"schedule_area_id": read_id, "status": read["status"],
                "removed": 0, "restored": 0}
    document_id = read["document_id"]
    removed = restored = 0
    if read["status"] == "ready":
        replaced = read.get("replaced") or {}
        if isinstance(replaced, list):  # recorded before tables were taken over
            replaced = {"doors": replaced, "tables": []}
        gone = (db.table("doors").delete().eq("schedule_area_id", read_id)
                .execute().data or [])
        removed = len(gone)
        doors = (db.table("doors").select("id, door_tag, level")
                 .eq("document_id", document_id).execute().data or [])
        # The boxes placed on the plans for the doors this read added go with
        # them -- unless another door still carries the number. Boxes a person
        # placed are theirs and stay.
        left = {d["door_tag"] for d in doors if d.get("door_tag")}
        orphans = sorted({d["door_tag"] for d in gone if d.get("door_tag")} - left)
        for start in range(0, len(orphans), store._BATCH):
            db.table("detections").delete().eq("document_id", document_id).in_(
                "door_tag", orphans[start:start + store._BATCH]).execute()
        for change in replaced.get("doors") or []:
            door = _match(doors, change["door_tag"], change.get("level") or "")
            if door is None:
                continue
            db.table("doors").update(change["before"]).eq("id", door["id"]).execute()
            restored += 1
        # Tables taken over say what they said before; then whatever still
        # names this read is a table it added, and comes off.
        for table in replaced.get("tables") or []:
            db.table("schedules").update(table["before"]).eq("id", table["id"]).execute()
        db.table("schedules").delete().eq("schedule_area_id", read_id).execute()
        # Tables this read took off the removed list go back on it.
        back = replaced.get("unremoved") or []
        if back:
            now = store.removed_schedules(db, document_id)
            db.table("documents").update(
                {"removed_schedules": now + [g for g in back if g not in now]}
            ).eq("id", document_id).execute()
    update(read_id, status="undone", undone_at=_now())
    log.info("db: schedule area read %s undone: %d door(s) removed, %d restored",
             read_id, removed, restored)
    return {"schedule_area_id": read_id, "status": "undone",
            "removed": removed, "restored": restored}


def shown(read: dict[str, Any]) -> dict[str, Any]:
    """A read as a caller sees it: the tables' rows left out, counts kept."""
    out = {k: v for k, v in read.items() if k not in ("replaced", "tables")}
    tables = read.get("tables") or []
    out["tables"] = [{k: v for k, v in t.items() if k != "rows"} for t in tables]
    return out
