"""Read the door schedule table a person drew around.

    POST   /documents/{id}/schedule-areas    draw one or more boxes; answered with a job
    GET    /documents/{id}/schedule-areas    every schedule area read on the set
    GET    /schedule-areas/{read_id}         one read and how it is going
    DELETE /schedule-areas/{read_id}         undo it

One box is one whole table: its headings, every section and the gaps between
them. Two tables are two boxes. Boxes that overlap are read once.

Each box is read from the PDF's own text first; where it holds no text to read
(a scan) or the text read is plainly partial, a picture of the box alone goes
to the image reader. Doors the schedule did not have are added, doors it had
take the values found, and undo puts it back as it was. A later read of the
whole schedule keeps these.
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api import storage
from app.api.deps import Caller, require_caller
from app.api.offload import in_worker
from app.config import get_settings
from app.core.pdf_doc import PdfDoc
from app.core.read_area import normalise
from app.core.read_schedule_area import finish as finish_tables
from app.core.read_schedule_area import merge_boxes, read_box
from app.db import files, schedule_areas, store

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["schedule areas"])

# Boxes read in one request. More than this many tables is a whole schedule,
# which the schedule read is for.
_MAX_BOXES = 20

# What a person is told. What happened, never why.
_FAILED = "Reading this table failed. Please try again."
_NOTHING = "No door schedule rows were found in this area."
_SAME = "Every door in this table is already in the schedule as read."
_PART = "Part of what was drawn could not be read. Draw around it again to finish it."
_NO_IMAGES = ("This table could not be read as text, and image reading is not "
              "set up on this server.")


class DrawnTable(BaseModel):
    """A box drawn around one whole table, as fractions of the page as the
    viewer shows it. Any corner order; anything past the edge is clipped."""

    page: int = Field(ge=1, description="Page number in the schedule's file, from 1")
    x0: float
    y0: float
    x1: float
    y1: float


class ScheduleAreaRequest(BaseModel):
    areas: list[DrawnTable] = Field(
        min_length=1, max_length=_MAX_BOXES,
        description="One box per table, around the whole table -- headings, "
        "every section and the gaps between them. Boxes that overlap are read "
        "once.")


@router.post("/documents/{document_id}/schedule-areas",
             status_code=status.HTTP_202_ACCEPTED)
async def read_schedule_areas(document_id: str, body: ScheduleAreaRequest,
                              caller: Caller = Depends(require_caller)):
    """Read the door schedule inside the boxes drawn.

    Answers at once with a read id and a job id; poll
    GET /schedule-areas/{read_id} or GET /jobs/{job_id}. When it is ready the
    doors are in the schedule. DELETE /schedule-areas/{read_id} undoes it.
    Asked while another schedule area read on the set runs, it waits its turn
    (status `queued`).
    """
    db = storage._db()
    found = store.stored_pdf(document_id)
    if not found:
        raise HTTPException(status_code=404, detail="No such document.")
    # The schedule's own file: on a set that came as two files the drawings
    # are kept apart, and the schedule is in the one it arrived as.
    key = found.get("source_uri")
    if not key:
        raise HTTPException(
            status_code=409,
            detail="That set's file was not kept, so its schedule cannot be read "
                   "again. Upload it again to read part of a sheet.")
    if not files.available():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="No file store configured.")

    pages = sorted({a.page for a in body.areas})
    counted = (db.table("documents").select("page_count")
               .eq("id", document_id).execute().data)
    page_count = counted[0].get("page_count") if counted else None
    beyond = [p for p in pages if page_count and p > page_count]
    if beyond:
        raise HTTPException(
            status_code=422,
            detail=f"This set has {page_count} pages; there is no page "
                   f"{', '.join(map(str, beyond))}.")

    drawn = []
    for a in body.areas:
        box = normalise(a.x0, a.y0, a.x1, a.y1)
        if box[2] - box[0] <= 0 or box[3] - box[1] <= 0:
            raise HTTPException(status_code=422,
                                detail="A box drawn has no width or no height.")
        drawn.append({"page": a.page, "box": list(box)})

    job_id = None
    try:
        job_id = store.create_job(found["project_id"], kind="schedule_area",
                                  source_key=key, filename=None,
                                  document_id=document_id, status="queued")
    except Exception as exc:  # noqa: BLE001 - the read still runs without one
        log.warning("schedule area job not recorded for %s: %s", document_id, exc)
    try:
        read = schedule_areas.create(document_id, drawn, created_by=caller.user_id,
                                     job_id=job_id)
    except Exception as exc:  # noqa: BLE001 - before db/038
        log.error("schedule area read not recorded for %s: %s", document_id, exc)
        if job_id:
            store.finish_job(job_id, "failed", message=_FAILED, document_id=document_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Reading part of a schedule is not set up on this server yet.") from exc

    _read_later(read["id"], document_id, key, job_id, drawn)
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED,
                        content={"schedule_area_id": read["id"], "job_id": job_id,
                                 "status": "queued", "drawn": drawn})


async def _read_all(path, drawn: list[dict], ai: bool):
    """Every box read, in order. Returns (tables, method, warnings, unread)."""
    from app.ai import spend

    spend.start()
    boxes = merge_boxes([(d["page"], tuple(d["box"])) for d in drawn])
    tables, methods, warnings, unread = [], set(), [], 0
    with PdfDoc(path) as doc:
        for page, box in boxes:
            if not 1 <= page <= doc.page_count:
                unread += 1
                continue
            read = await read_box(doc, page, box, ai=ai)
            if not read.tables:
                unread += 1
                continue
            tables += read.tables
            methods.add(read.method)
            warnings += read.warnings
    tables = finish_tables(tables, warnings)
    method = "image" if "image" in methods else ("text" if methods else "")
    return tables, method, warnings, unread, len(boxes)


def _read_later(read_id: str, document_id: str, key: str, job_id: str | None,
                drawn: list[dict]) -> None:
    """Read the boxes after the request has been answered, and record how it
    went on the read and its job."""

    def finish(state: str, message: str | None, doors: int | None) -> None:
        schedule_areas.update(read_id, status=state, message=message)
        if job_id:
            try:
                store.finish_job(job_id, "ready" if state in ("ready", "undone")
                                 else "failed", message=message,
                                 document_id=document_id, doors=doors,
                                 needs_review=bool(message) and state == "ready")
            except Exception:  # noqa: BLE001 - the stale check covers it
                pass

    async def run() -> None:
        path = None
        started = time.perf_counter()
        try:
            await store.wait_turn(job_id, document_id, kind="schedule_area")
            if (schedule_areas.get(read_id) or {}).get("status") == "undone":
                finish("undone", None, 0)
                return
            schedule_areas.update(read_id, status="reading")
            started = time.perf_counter()
            path = await asyncio.to_thread(files.fetch, key)
            ai = get_settings().ai_enabled
            try:
                tables, method, warnings, unread, boxes = await in_worker(_read_all(path, drawn, ai))
            except Exception as exc:  # noqa: BLE001 - incl. the provider refusing
                log.warning("schedule area read %s could not be read: %s", read_id, exc)
                finish("failed", _FAILED, None)
                return
            if (schedule_areas.get(read_id) or {}).get("status") == "undone":
                finish("undone", None, 0)
                return
            if not tables:
                finish("ready", _NOTHING if ai else _NO_IMAGES, 0)
                return
            saved = schedule_areas.save(document_id, read_id, tables, method)
            if saved is None:
                finish("failed", _FAILED, None)
                return
            note = _PART if unread else None
            if not saved["added"] and not saved["updated"]:
                note = f"{_SAME} {note}" if note else _SAME
            finish("ready", note, saved["added"] + saved["updated"])
            log.info("schedule area read %s on %s: %d box(es), %d added, %d "
                     "updated in %.0f s", read_id, document_id, boxes,
                     saved["added"], saved["updated"], time.perf_counter() - started)
        except Exception as exc:  # noqa: BLE001 - the schedule stands as it was
            log.warning("schedule area read %s failed after %.0f s: %s", read_id,
                        time.perf_counter() - started, exc)
            finish("failed", _FAILED, None)
        finally:
            if path is not None:
                files.discard(path)

    task = asyncio.get_running_loop().create_task(run())
    storage._PLAN_PASSES.add(task)
    task.add_done_callback(storage._PLAN_PASSES.discard)


@router.get("/documents/{document_id}/schedule-areas")
async def list_schedule_areas(document_id: str,
                              caller: Caller = Depends(require_caller)):
    """Every schedule area read on a set, newest first, undone ones included."""
    storage._db()
    try:
        found = schedule_areas.for_document(document_id)
    except Exception as exc:  # noqa: BLE001 - before db/038
        log.info("schedule area reads not listed (%s)", exc)
        found = []
    return {"document_id": document_id,
            "schedule_areas": [schedule_areas.shown(r) for r in found]}


@router.get("/schedule-areas/{read_id}")
async def get_schedule_area(read_id: str, caller: Caller = Depends(require_caller)):
    """One schedule area read: what was drawn, what was read, how it is going."""
    storage._db()
    read = schedule_areas.get(read_id)
    if read is None:
        raise HTTPException(status_code=404, detail="No such schedule area read.")
    return schedule_areas.shown(read)


@router.delete("/schedule-areas/{read_id}")
async def undo_schedule_area(read_id: str, caller: Caller = Depends(require_caller)):
    """Undo a schedule area read: the doors it added go, the values it changed
    come back, the tables it made come off. Undone while still being read,
    nothing is written. Undoing twice is the same as once."""
    storage._db()
    done = schedule_areas.undo(read_id)
    if done is None:
        raise HTTPException(status_code=404, detail="No such schedule area read.")
    return done
