"""Schedule area reads: the door schedule tables a person drew around, stored.

What a read does to the schedule:

  added      a door number the schedule did not have becomes a door, tied to
             this read (doors.schedule_area_id) and to a table of its own
  updated    a door the schedule already had takes the values this read found
             for it -- only the ones it found; an empty cell never blanks a
             value. What it held before is kept on the read, for undo.
             A person's corrections are never touched: they live apart, keyed
             on the door number, and stand over whatever a read says.
  kept       a read of the whole schedule replaces every door, and would wipe
             this. After one, every read still standing is applied again, oldest
             first -- see `reapply`, called from `store.save_extraction`.
  undone     the doors it added go, the values it replaced come back, and the
             tables it made are taken off.

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
# Door fields a read may set. `extra` is merged, never replaced.
_FIELDS = tuple(f for f in DoorRow.model_fields if f not in ("extra", "door_tag", "level"))


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

def _tables_json(tables: list[ScheduleTable]) -> list[dict]:
    """The tables as stored on the read, rows included, so the read can be
    applied again after the whole schedule is re-read."""
    return [t.model_dump(mode="json") for t in tables]


def _tables_from(stored: list[dict]) -> list[ScheduleTable]:
    return [ScheduleTable(**t) for t in stored or []]


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


def _apply(db, document_id: str, org_id: str, read_id: str,
           tables: list[ScheduleTable]) -> dict[str, Any]:
    """Write one read's tables into the schedule. Returns what it did."""
    removed = store.removed_schedules(db, document_id)
    tables = [t for t in tables
              if not any(store._same_schedule(g, t.page, t.title) for g in removed)]
    made = store._insert("schedules", [
        {"document_id": document_id, "page": t.page, "title": t.title,
         "headers": t.headers, "field_map": t.field_map, "row_count": t.row_count,
         "box_x0": t.box.x0 if t.box else None, "box_y0": t.box.y0 if t.box else None,
         "box_x1": t.box.x1 if t.box else None, "box_y1": t.box.y1 if t.box else None,
         "box_source": t.box.source if t.box else None,
         "schedule_area_id": read_id}
        for t in tables
    ]) if tables else []

    existing = (db.table("doors").select("*").eq("document_id", document_id)
                .execute().data or [])
    last = max((d.get("row_index") or 0 for d in existing), default=-1)
    added, updated, seen, untagged = [], [], set(), 0
    for table, schedule in zip(tables, made):
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
                              "schedule_id": schedule["id"], "row_index": last,
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
            "tables": len(made)}


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
        update(read_id, tables=_tables_json(tables), replaced=done["updated"],
               added=done["added"], updated=len(done["updated"]), method=method)
        log.info("db: schedule area read %s: %d door(s) added, %d updated",
                 read_id, done["added"], len(done["updated"]))
        return {"added": done["added"], "updated": len(done["updated"]),
                "untagged": done["untagged"]}
    except NoDatabase:
        return None
    except Exception as exc:  # noqa: BLE001 - the schedule stands as it was
        log.warning("db: could not store schedule area read %s: %s", read_id, exc)
        return None


def reapply(document_id: str) -> int:
    """After the whole schedule is read again: put every standing area read
    back, oldest first, so a later read still wins where two overlap. Each
    read's undo now puts back what this re-read found. Returns how many."""
    try:
        db = client()
        standing = (db.table(TABLE).select("*").eq("document_id", document_id)
                    .eq("status", "ready").order("created_at").execute().data)
    except Exception as exc:  # noqa: BLE001 - before db/038 there are none
        log.info("db: no schedule area reads to keep (%s)", exc)
        return 0
    if not standing:
        return 0
    org = (db.table("documents").select("org_id").eq("id", document_id)
           .execute().data)
    if not org:
        return 0
    for read in standing:
        try:
            done = _apply(db, document_id, org[0]["org_id"], read["id"],
                          _tables_from(read.get("tables")))
            update(read["id"], replaced=done["updated"], added=done["added"],
                   updated=len(done["updated"]))
        except Exception as exc:  # noqa: BLE001 - one read must not stop the rest
            log.warning("db: schedule area read %s not kept: %s", read["id"], exc)
    log.info("db: %d schedule area read(s) kept through the re-read", len(standing))
    return len(standing)


def undo(read_id: str) -> dict[str, Any] | None:
    """Take a read off the schedule. Undoing twice is the same as once."""
    db = client()
    read = get(read_id)
    if read is None:
        return None
    if read["status"] == "undone":
        return {"schedule_area_id": read_id, "status": "undone",
                "removed": 0, "restored": 0}
    document_id = read["document_id"]
    removed = restored = 0
    if read["status"] == "ready":
        gone = (db.table("doors").delete().eq("schedule_area_id", read_id)
                .execute().data or [])
        removed = len(gone)
        doors = (db.table("doors").select("id, door_tag, level")
                 .eq("document_id", document_id).execute().data or [])
        for change in read.get("replaced") or []:
            door = _match(doors, change["door_tag"], change.get("level") or "")
            if door is None:
                continue
            db.table("doors").update(change["before"]).eq("id", door["id"]).execute()
            restored += 1
        db.table("schedules").delete().eq("schedule_area_id", read_id).execute()
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
