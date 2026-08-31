"""Writing a takeoff down, and reading it back.

The backend is the only writer. What the extractor found is a record of what was
on the drawing, and a record nobody can edit from a browser is the only kind
worth keeping -- see `corrections` for the part a person *is* allowed to change.

Two rules run through this file:

  storing must never break a takeoff   every write is best-effort. A database
                                       that is down, full or misconfigured
                                       costs you the memory of a run, not the
                                       run. The caller still gets its answer.

  re-reading replaces, never adds      a document has one set of doors. Reading
                                       it again after the extractor improves
                                       replaces them, so "which of these is the
                                       door?" always has one answer.
"""

from __future__ import annotations

import hashlib
import logging
import math
from typing import Any

from app.config import get_settings
from app.db.client import NoDatabase, client
from app.schemas import ExtractionResult, PlanAudit

log = logging.getLogger(__name__)

# Rows per insert. PostgREST takes a list in one call; a few hundred keeps the
# request from growing large enough to be rejected on a big set.
_BATCH = 500


def digest(data: bytes) -> str:
    """A file's identity. The same set arrives under a new name every week."""
    return hashlib.sha256(data).hexdigest()


def digest_path(path) -> tuple[str, int]:
    """The same, read from disk in blocks. Returns (sha256, size).

    Uploads are streamed to a temp file precisely so a 500 MB set is never held
    in memory; hashing it by reading it back in one go would undo that.
    """
    total = 0
    running = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(1024 * 1024):
            running.update(block)
            total += len(block)
    return running.hexdigest(), total


# --------------------------------------------------------------------------- #
# organisation and project
# --------------------------------------------------------------------------- #

def ensure_org(name: str) -> str:
    """The organisation, created if this is the first time it is mentioned."""
    db = client()
    found = db.table("organisations").select("id").eq("name", name).execute()
    if found.data:
        return found.data[0]["id"]
    made = db.table("organisations").insert({"name": name}).execute()
    log.info("db: created organisation %r", name)
    return made.data[0]["id"]


def ensure_project(org_id: str, name: str, code: str | None = None,
                   created_by: str | None = None) -> str:
    """The job, created if new. Named by a person, so matched by name."""
    db = client()
    found = (db.table("projects").select("id")
             .eq("org_id", org_id).eq("name", name).execute())
    if found.data:
        return found.data[0]["id"]
    row = {"org_id": org_id, "name": name}
    if code:
        row["code"] = code
    if created_by:
        row["created_by"] = created_by
    made = db.table("projects").insert(row).execute()
    log.info("db: created project %r", name)
    return made.data[0]["id"]


def find_document(project_id: str, sha256: str) -> dict[str, Any] | None:
    """This exact file in this job, if it has been read before.

    Scoped to the project on purpose. The same bytes in two jobs are two
    documents, so a file dropped into the wrong project is one row to delete
    rather than shared state to untangle.
    """
    db = client()
    found = (db.table("documents").select("*")
             .eq("project_id", project_id).eq("sha256", sha256).execute())
    return found.data[0] if found.data else None


def stored_pdf(document_id: str) -> dict[str, Any] | None:
    """Where a document's original drawing set lives, if it is still on file.

    `source_uri` is null for a set that arrived as a multipart upload: it was
    read from a temp file and that file is gone. Only the signed-link route
    keeps the bytes, and only those documents can be re-rendered later.
    """
    db = client()
    found = (db.table("documents")
             .select("id,filename,project_id,source_uri")
             .eq("id", document_id).execute())
    return found.data[0] if found.data else None


def set_document_status(document_id: str, status: str) -> bool:
    """Archive or restore one drawing set. False if there is no such document.

    Nothing is thrown away: the doors, sheets and detections stay exactly as
    they were, and so does the file. Restoring is the same call the other way
    round, which is the whole reason archiving is the default.
    """
    db = client()
    done = (db.table("documents").update({"status": status})
            .eq("id", document_id).execute())
    return bool(done.data)


def set_project_status(project_id: str, status: str) -> bool:
    """Archive or restore a whole job, documents included.

    The documents are set too rather than left to be inferred from the project,
    so that `documents.status` means one thing everywhere and a query never has
    to join to find out whether a set counts.
    """
    db = client()
    done = (db.table("projects").update({"status": status})
            .eq("id", project_id).execute())
    if not done.data:
        return False
    db.table("documents").update({"status": status}) \
        .eq("project_id", project_id).execute()
    return True


def stored_keys(project_id: str) -> list[str]:
    """Every stored file under a job, for deleting them alongside the rows."""
    db = client()
    found = (db.table("documents").select("source_uri")
             .eq("project_id", project_id).execute())
    return [r["source_uri"] for r in found.data if r.get("source_uri")]


def delete_document(document_id: str) -> bool:
    """Destroy one drawing set and everything read from it.

    The schedule, sheets, detections and corrections go with it -- every table
    referencing `documents` is `on delete cascade`, so this is one statement
    and cannot half-succeed.
    """
    db = client()
    done = db.table("documents").delete().eq("id", document_id).execute()
    return bool(done.data)


def delete_project(project_id: str) -> bool:
    """Destroy a job and every document under it.

    This also takes the job's `run_log` rows, which are the record of what was
    spent reading it. That history cannot be reconstructed -- the runs are gone
    and the money is not coming back -- which is why the endpoint asks twice.
    """
    db = client()
    done = db.table("projects").delete().eq("id", project_id).execute()
    return bool(done.data)


def _upsert_document(org_id: str, project_id: str, *, sha256: str,
                     filename: str, size_bytes: int, page_count: int | None,
                     revision: str | None, source_uri: str | None) -> str:
    db = client()
    existing = find_document(project_id, sha256)
    row = {
        "org_id": org_id,
        "project_id": project_id,
        "sha256": sha256,
        "filename": filename,
        "size_bytes": size_bytes,
        "page_count": page_count,
        "revision": revision,
        "source_uri": source_uri,
    }
    if existing:
        db.table("documents").update(row).eq("id", existing["id"]).execute()
        return existing["id"]
    return db.table("documents").insert(row).execute().data[0]["id"]


def _insert(table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    db = client()
    for start in range(0, len(rows), _BATCH):
        chunk = rows[start:start + _BATCH]
        out.extend(db.table(table).insert(chunk).execute().data or [])
    return out


# --------------------------------------------------------------------------- #
# the schedule
# --------------------------------------------------------------------------- #

def save_extraction(*, org: str = "", project: str = "", filename: str,
                    result: ExtractionResult, sha256: str, size_bytes: int,
                    project_id: str | None = None, org_id: str | None = None,
                    revision: str | None = None,
                    source_uri: str | None = None) -> str | None:
    """Store a schedule reading. Returns the document id, or None if not stored.

    Never raises for a database reason. A takeoff that cannot be written down is
    still a takeoff, and the caller has already been given it.
    """
    try:
        # Either the ids are known -- the caller came in through a project
        # endpoint -- or they are named and made if missing, which is what a
        # script or a first run does.
        if not project_id:
            org_id = ensure_org(org)
            project_id = ensure_project(org_id, project)
        elif not org_id:
            got = (client().table("projects").select("org_id")
                   .eq("id", project_id).execute())
            if not got.data:
                log.warning("db: no project %s; not storing", project_id)
                return None
            org_id = got.data[0]["org_id"]

        document_id = _upsert_document(
            org_id, project_id, sha256=sha256, filename=filename,
            size_bytes=size_bytes, page_count=result.pages_scanned,
            revision=revision, source_uri=source_uri)

        db = client()
        # Replace, never add. See the note at the top of this file.
        db.table("doors").delete().eq("document_id", document_id).execute()
        db.table("schedules").delete().eq("document_id", document_id).execute()

        tables = result.tables or []
        made = _insert("schedules", [
            {"document_id": document_id, "page": t.page, "title": t.title,
             "headers": t.headers, "field_map": t.field_map,
             "row_count": t.row_count}
            for t in tables
        ])
        schedule_id = {t.page: m["id"] for t, m in zip(tables, made)}

        rows, seen, repeated = [], set(), []
        for index, door in enumerate(result.rows):
            tag = door.door_tag or None
            # `doors` allows one row per number, and that constraint is doing
            # real work: one set's damaged font read doors 106 and 108 both as
            # "10". Refusing the whole save over it would lose eighty good rows,
            # so the repeat is dropped and named in the log instead.
            if tag and tag in seen:
                repeated.append(tag)
                continue
            if tag:
                seen.add(tag)
            values = door.model_dump(exclude={"extra"})
            rows.append({
                "org_id": org_id, "document_id": document_id,
                "schedule_id": schedule_id.get(
                    tables[0].page if tables else 0),
                "row_index": index, **values, "extra": door.extra,
            })
        _insert("doors", rows)

        if repeated:
            log.warning("db: %d door number(s) appear twice and were stored "
                        "once: %s", len(repeated), ", ".join(repeated[:8]))
        log.info("db: stored %d door(s) for %s", len(rows), filename)
        _log_run(org_id, project_id, document_id, "extract", result=result,
                 doors=len(rows))
        return document_id
    except NoDatabase:
        return None
    except Exception as exc:  # noqa: BLE001 - storing must not break a takeoff
        log.warning("db: could not store the schedule for %s: %s", filename, exc)
        return None


# --------------------------------------------------------------------------- #
# the drawings
# --------------------------------------------------------------------------- #

# How near two boxes must be to be the same box, in box-widths between their
# centres. Half a width: clearly closer than the one-leaf distance at which the
# plan pipeline decides an arc belongs to the next door along, so suppressing a
# repeat can never suppress its neighbour.
#
# Measured in box-widths rather than points because detections are stored as
# page fractions, and a fraction means different distances on a 24x36 sheet and
# an 11x17. A door box is a door box on both.
_SAME_BOX = 0.5


def _centre(box: dict) -> tuple[float, float]:
    return ((box["x0"] + box["x1"]) / 2, (box["y0"] + box["y1"]) / 2)


def is_suppressed(det: dict, tombstones: list[dict]) -> bool:
    """Has a person already removed this box?

    By number where there is one: a tagged door that comes back on the same
    sheet is the same door, wherever the box landed this time. By position
    where there is not -- an untagged box has no other handle, and untagged is
    most of what gets deleted, because a circle fitted to a structural column
    has no number by definition.
    """
    for dead in tombstones:
        if dead["page"] != det.get("page"):
            continue
        if dead.get("door_tag") and det.get("door_tag"):
            if dead["door_tag"] == det["door_tag"]:
                return True
            continue
        if dead.get("door_tag") or det.get("door_tag"):
            continue
        dx, dy = _centre(dead), _centre(det)
        width = max(dead["x1"] - dead["x0"], det["x1"] - det["x0"], 1e-6)
        if math.hypot(dx[0] - dy[0], dx[1] - dy[1]) <= width * _SAME_BOX:
            return True
    return False


def tombstones(document_id: str) -> list[dict[str, Any]]:
    """Boxes a person has removed, which must not come back."""
    try:
        db = client()
        return (db.table("detection_tombstones").select("*")
                .eq("document_id", document_id).execute().data)
    except NoDatabase:
        return []
    except Exception as exc:  # noqa: BLE001 - a missing table must not stop an audit
        log.warning("db: could not read tombstones: %s", exc)
        return []


def manual_detections(document_id: str) -> list[dict[str, Any]]:
    """Boxes a person placed. Never rebuilt, never wiped by an audit."""
    try:
        db = client()
        return (db.table("manual_detections").select("*")
                .eq("document_id", document_id).execute().data)
    except NoDatabase:
        return []
    except Exception as exc:  # noqa: BLE001
        log.warning("db: could not read manual detections: %s", exc)
        return []


def save_audit(document_id: str, audit: PlanAudit) -> bool:
    """Store where each door was found and what its swing measures."""
    try:
        db = client()
        document = (db.table("documents").select("id, org_id, project_id")
                    .eq("id", document_id).execute())
        if not document.data:
            return False
        org_id = document.data[0]["org_id"]

        db.table("detections").delete().eq("document_id", document_id).execute()
        db.table("sheets").delete().eq("document_id", document_id).execute()

        made = _insert("sheets", [
            {"document_id": document_id, "page": s.page, "number": s.number,
             "title": s.title, "level": s.level, "leads": s.leads,
             "scanned": s.scanned, "is_enlargement": s.is_enlargement,
             "width_pt": s.width, "height_pt": s.height}
            for s in audit.floor_plans
        ])
        sheet_id = {s.page: m["id"] for s, m in zip(audit.floor_plans, made)}

        # Boxes a person has already removed. The geometry pass is
        # deterministic, so without this the same wrong box is written back on
        # every audit and removed again by hand on every audit.
        dead = tombstones(document_id)
        rows, seen, suppressed = [], set(), 0
        for door in audit.detected:
            page = door.location.page
            if dead and is_suppressed(
                    {"page": page, "door_tag": door.tag or None,
                     "x0": door.location.x0, "y0": door.location.y0,
                     "x1": door.location.x1, "y1": door.location.y1}, dead):
                suppressed += 1
                continue
            if page not in sheet_id:
                # Drawn on a sheet the audit did not list as a floor plan.
                # Nothing to hang it on, and inventing a sheet row would put a
                # door on a drawing we never said was one.
                continue
            key = (door.tag or "", sheet_id[page])
            if key in seen:
                continue
            seen.add(key)
            arc = door.arc
            rows.append({
                "org_id": org_id, "document_id": document_id,
                "sheet_id": sheet_id[page], "door_tag": door.tag or None,
                "x0": door.location.x0, "y0": door.location.y0,
                "x1": door.location.x1, "y1": door.location.y1,
                "kind": door.type, "swing": door.swing or None,
                "source": door.source, "confidence": door.confidence or None,
                "measured_width": door.measured_width or None,
                "is_primary": door.primary, "sheet_scale": door.sheet_scale,
                "also_on": door.also_on,
                "hinge_x": arc.hinge_x if arc else None,
                "hinge_y": arc.hinge_y if arc else None,
                "radius": arc.radius if arc else None,
                "start_deg": arc.start_deg if arc else None,
                "end_deg": arc.end_deg if arc else None,
                "residual": arc.residual if arc else None,
                "other_leaf": (door.other_leaf.model_dump()
                               if door.other_leaf else None),
            })
        _insert("detections", rows)
        if suppressed:
            log.info("db: %d detection(s) left out -- removed by hand earlier",
                     suppressed)
        log.info("db: stored %d detection(s) on %d sheet(s)",
                 len(rows), len(made))
        _log_run(org_id, document.data[0]["project_id"], document_id, "audit",
                 audit=audit, doors=audit.door_count)
        return True
    except NoDatabase:
        return False
    except Exception as exc:  # noqa: BLE001 - storing must not break an audit
        log.warning("db: could not store the audit: %s", exc)
        return False


# --------------------------------------------------------------------------- #
# what it cost and found
# --------------------------------------------------------------------------- #

def _log_run(org_id: str, project_id: str, document_id: str, kind: str, *,
             result: ExtractionResult | None = None,
             audit: PlanAudit | None = None, doors: int = 0) -> None:
    """One summary row per read. Nothing joins to it.

    That is the point: it keeps the history that makes "did last night's change
    help?" answerable, without putting a second copy of every door in the
    database and reopening the question of which copy is the door.
    """
    settings = get_settings()
    row: dict[str, Any] = {
        "org_id": org_id, "project_id": project_id, "document_id": document_id,
        "kind": kind, "app_version": settings.app_version,
        "doors_scheduled": doors,
    }
    if result is not None:
        row.update(method=result.method.value,
                   pages_scanned=result.pages_scanned,
                   duration_ms=result.duration_ms,
                   warnings=result.warnings)
    if audit is not None:
        counted = [d for d in audit.detected if d.primary]
        row.update(pages_scanned=audit.pages_scanned,
                   duration_ms=audit.duration_ms,
                   doors_located=len(audit.located),
                   swings_measured=sum(1 for d in counted if d.arc),
                   warnings=audit.warnings)
        if audit.scan_cost:
            row.update(model=audit.scan_cost.model,
                       cost_usd=audit.scan_cost.estimated_usd,
                       tiles_sent=audit.scan_cost.tiles_sent)
    try:
        client().table("run_log").insert(row).execute()
    except Exception as exc:  # noqa: BLE001 - a missing log entry is not fatal
        log.warning("db: could not write the run log: %s", exc)
