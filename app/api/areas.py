"""Read the doors in a part of a plan a person drew around.

    POST   /documents/{id}/areas     draw one or more boxes; answered with a job
    GET    /documents/{id}/areas     every area read on the set
    GET    /areas/{area_id}          one area read and how it is going
    DELETE /areas/{area_id}          undo it

The doors found appear straight away; undo puts back what they replaced.
Whatever is drawn is answered -- see app/core/read_area.py for how a box too
small, too big, overlapping or dragged past the edge is read. The only refusals
are real ones: no such set, its file not kept, image reading not set up, a page
the set does not have, a set with no schedule read yet.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api import storage
from app.api.deps import Caller, require_caller
from app.api.offload import in_worker
from app.config import get_settings
from app.core.pdf_doc import PdfDoc
from app.core.read_area import WHOLE, AreaPlan, keep, normalise, plan_areas
from app.db import areas, files, store
from app.plan_pipeline import audit
from app.schemas import PlanAudit

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["areas"])

# Sheets read in one pass of the vision reader. More are read in turns within
# the same job, so a person drawing on twenty sheets is answered, not refused.
_PAGES_PER_PASS = 10

# What a person is told. What happened, never why.
_FAILED = "Reading this area failed. Please try again."
_NOTHING = "No doors were found in this area."
_NOT_FOUND = "The doors chosen were not found in this area."


class DrawnArea(BaseModel):
    """A box a person drew, as fractions of the page as the viewer shows it.
    Any corner order; anything past the edge is clipped to the page."""

    page: int = Field(ge=1, description="Page number in the drawing set, from 1")
    x0: float
    y0: float
    x1: float
    y1: float


class AreaRequest(BaseModel):
    areas: list[DrawnArea] = Field(
        min_length=1,
        description="The boxes drawn. Several at once are read together; "
        "boxes that overlap or touch are read once.")
    doors: list[str] = Field(
        default_factory=list,
        description="Door numbers to read in the boxes, from the schedule. "
        "Left out, every door in them is read. Given, only those doors' boxes "
        "in the area are replaced; every other box there stays.")
    budget_usd: float | None = Field(None, gt=0, description="Ceiling for this read")


def _shown(area: dict[str, Any]) -> dict[str, Any]:
    """An area read as a caller sees it: without the boxes kept for undo."""
    out = {k: v for k, v in area.items() if k != "replaced"}
    out["replaced_count"] = len(area.get("replaced") or [])
    return out


@router.post("/documents/{document_id}/areas", status_code=status.HTTP_202_ACCEPTED)
async def read_areas(document_id: str, body: AreaRequest,
                     caller: Caller = Depends(require_caller)):
    """Read the doors inside the boxes drawn on a plan, with the vision model.

    Answers at once with an area id and a job id; poll GET /areas/{area_id} or
    GET /jobs/{job_id}. When it is ready the doors are on the sheet -- the
    boxes that stood there before are replaced, and DELETE /areas/{area_id}
    puts them back. Asked while another plan read on the set runs, it waits
    its turn (status `queued`).
    """
    if not get_settings().ai_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Image reading is not set up on this server.")
    db = storage._db()
    found = store.stored_pdf(document_id)
    if not found:
        raise HTTPException(status_code=404, detail="No such document.")
    key = store.drawings_uri(found)
    if not key:
        raise HTTPException(
            status_code=409,
            detail="That set's file was not kept, so its plans cannot be read. "
                   "Upload it again to read part of a sheet.")
    if not files.available():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="No file store configured.")

    pages = sorted({a.page for a in body.areas})
    if not found.get("plans_uri"):
        counted = (db.table("documents").select("page_count")
                   .eq("id", document_id).execute().data)
        page_count = counted[0].get("page_count") if counted else None
        beyond = [p for p in pages if page_count and p > page_count]
        if beyond:
            raise HTTPException(
                status_code=422,
                detail=f"This set has {page_count} pages; there is no page "
                       f"{', '.join(map(str, beyond))}.")

    rows = storage._stored_rows(db, document_id)
    if not rows:
        raise HTTPException(
            status_code=422,
            detail="This set has no door schedule read yet. Read the schedule "
                   "first, then the doors on the plans.")
    doors = list(dict.fromkeys(t.strip() for t in body.doors if t and t.strip()))
    known = {r.door_tag for r in rows if r.door_tag}
    unknown = [t for t in doors if t not in known]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Not in this schedule: {', '.join(unknown)}.")

    drawn = [{"page": a.page, "box": list(normalise(a.x0, a.y0, a.x1, a.y1))}
             for a in body.areas]
    job_id = None
    try:
        job_id = store.create_job(found["project_id"], kind="plan", source_key=key,
                                  filename=None, document_id=document_id,
                                  status="queued")
    except Exception as exc:  # noqa: BLE001 - the read still runs without one
        log.warning("area job not recorded for %s: %s", document_id, exc)
    try:
        area = areas.create(document_id, drawn, created_by=caller.user_id,
                            job_id=job_id, doors=doors)
    except Exception as exc:  # noqa: BLE001
        log.error("area read not recorded for %s: %s", document_id, exc)
        if job_id:
            store.finish_job(job_id, "failed", message=_FAILED, document_id=document_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Reading part of a sheet is not set up on this server yet.") from exc

    stored_pages = (db.table("schedules").select("page")
                    .eq("document_id", document_id).order("page").execute().data)
    schedule_page = stored_pages[0]["page"] if stored_pages else 0
    _read_later(area["id"], document_id, key, rows, schedule_page, job_id,
                drawn, body.budget_usd, doors)
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED,
                        content={"area_id": area["id"], "job_id": job_id,
                                 "status": "queued", "drawn": drawn,
                                 "doors": doors})


def _sizes(path, pages: list[int]) -> dict[int, tuple[float, float]]:
    with PdfDoc(path) as doc:
        return {p: doc.page_size(p - 1) for p in pages if 1 <= p <= doc.page_count}


def _read_later(area_id: str, document_id: str, key: str, rows, schedule_page: int,
                job_id: str | None, drawn: list[dict],
                budget_usd: float | None, doors: list[str] | None = None) -> None:
    """Read the area after the request has been answered, and record how it
    went on the area and its job."""

    def finish(state: str, message: str | None, doors: int | None) -> None:
        areas.update(area_id, status=state, message=message)
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
            await store.wait_turn(job_id, document_id)
            if (areas.get(area_id) or {}).get("status") == "undone":
                finish("undone", None, 0)
                return
            areas.update(area_id, status="reading")
            started = time.perf_counter()
            path = await asyncio.to_thread(files.fetch, key)

            pages = sorted({d["page"] for d in drawn})
            sizes = await asyncio.to_thread(_sizes, path, pages)
            plans = plan_areas([(d["page"], tuple(d["box"])) for d in drawn
                                if d["page"] in sizes], sizes)
            areas.update(area_id, plan=[p.as_json() for p in plans])
            if not plans:
                # Every page drawn on is past the end of the drawings file.
                finish("ready", _NOTHING, 0)
                return

            result = PlanAudit()
            read_pages = sorted({p.page for p in plans})
            for start in range(0, len(read_pages), _PAGES_PER_PASS):
                chunk = read_pages[start:start + _PAGES_PER_PASS]
                regions: dict[int, list[tuple[float, float, float, float]]] = {}
                for p in plans:
                    if p.page in chunk:
                        regions.setdefault(p.page, []).append(
                            WHOLE if p.whole else p.read)
                part = await in_worker(audit(path, rows=rows, detect=True,
                                             budget_usd=budget_usd,
                                             schedule_page=schedule_page,
                                             pages=chunk, doors=set(doors or []) or None,
                                             regions=regions))
                result.floor_plans += part.floor_plans
                result.detected += part.detected
                result.unread += part.unread

            if doors:
                # Only the doors asked for are an answer to this request.
                result.detected = [d for d in result.detected if d.tag in doors]
            result.detected = keep(result.detected, plans)
            if (areas.get(area_id) or {}).get("status") == "undone":
                # Undone while it was being read: nothing to put on the sheet.
                finish("undone", None, 0)
                return
            saved = areas.save(document_id, area_id, result, plans, doors=doors)
            if saved is None:
                finish("failed", _FAILED, None)
                return
            note = None if result.detected else (
                _NOT_FOUND if doors else _NOTHING)
            if result.unread:
                left = ("Part of this area could not be read; doors there may "
                        "be missing. Draw around it again to finish it.")
                note = f"{note} {left}".strip() if note else left
            finish("ready", note, saved["added"])
            log.info("area read %s on %s: %d door(s) in %.0f s", area_id,
                     document_id, saved["added"], time.perf_counter() - started)
        except Exception as exc:  # noqa: BLE001 - the sheet stands as it was
            log.warning("area read %s failed after %.0f s: %s", area_id,
                        time.perf_counter() - started, exc)
            finish("failed", _FAILED, None)
        finally:
            if path is not None:
                files.discard(path)

    task = asyncio.get_running_loop().create_task(run())
    storage._PLAN_PASSES.add(task)
    task.add_done_callback(storage._PLAN_PASSES.discard)


@router.get("/documents/{document_id}/areas")
async def list_areas(document_id: str, caller: Caller = Depends(require_caller)):
    """Every area read on a set, newest first, undone ones included."""
    storage._db()
    try:
        found = areas.for_document(document_id)
    except Exception as exc:  # noqa: BLE001 - before db/037
        log.info("area reads not listed (%s)", exc)
        found = []
    return {"document_id": document_id, "areas": [_shown(a) for a in found]}


@router.get("/areas/{area_id}")
async def get_area(area_id: str, caller: Caller = Depends(require_caller)):
    """One area read: what was drawn, what was read, how it is going."""
    storage._db()
    area = areas.get(area_id)
    if area is None:
        raise HTTPException(status_code=404, detail="No such area read.")
    return _shown(area)


@router.delete("/areas/{area_id}")
async def undo_area(area_id: str, caller: Caller = Depends(require_caller)):
    """Undo an area read: its doors come off the sheet and the boxes they
    replaced go back. Undone while still being read, nothing is put on the
    sheet. Undoing twice is the same as once."""
    storage._db()
    done = areas.undo(area_id)
    if done is None:
        raise HTTPException(status_code=404, detail="No such area read.")
    return done
