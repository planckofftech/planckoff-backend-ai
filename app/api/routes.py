from __future__ import annotations

import json
import logging
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app import __version__
from app.ai.client import AiUpstreamError
from app.api.deps import require_api_key
from app.api.offload import in_worker
from app.config import get_settings
from app.core.pdf_doc import NotAPdfError, PdfDoc
from app.pipeline import NoRowsError, NoScheduleFoundError, extract
from app.plan_pipeline import NoDoorScheduleError, audit
from app.schemas import DoorRow, ExtractionResult, HealthResponse, PlanAudit

log = logging.getLogger(__name__)
router = APIRouter()

_UPLOAD_CHUNK = 1024 * 1024
# Beyond this a sheet is solid red and the view stops informing anyone.
_MAX_MARKS = 200


@router.get("/health", response_model=HealthResponse, tags=["meta"])
async def health() -> HealthResponse:
    return HealthResponse(version=__version__, ai_enabled=get_settings().ai_enabled)


def _too_large(actual_mb: float | None, limit_mb: float) -> HTTPException:
    size = f"{actual_mb:.1f} MB" if actual_mb is not None else f"over {limit_mb:.0f} MB"
    return HTTPException(
        status_code=413,
        detail=f"PDF too large ({size}). Maximum is {limit_mb:.0f} MB.",
    )


@asynccontextmanager
async def _spooled_upload(file: UploadFile) -> AsyncIterator[Path]:
    """Stream the upload to a temp file and yield its path; always clean up.

    Buffering the upload into one blob and handing that to PyMuPDF meant the
    file sat in memory twice -- around a gigabyte for a 500 MB drawing set
    before a page was read. From disk, pages are read as they are needed.
    """
    settings = get_settings()
    limit = int(settings.max_upload_mb * 1024 * 1024)

    # Starlette knows the part size up front; reject before writing anything.
    declared = getattr(file, "size", None)
    if declared is not None and declared > limit:
        raise _too_large(declared / 1024 / 1024, settings.max_upload_mb)

    handle = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    path = Path(handle.name)
    total = 0
    try:
        try:
            while chunk := await file.read(_UPLOAD_CHUNK):
                total += len(chunk)
                if total > limit:
                    raise _too_large(total / 1024 / 1024, settings.max_upload_mb)
                handle.write(chunk)
        finally:
            handle.close()

        if not total:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="File is empty.")
        yield path
    finally:
        _discard(path)


def _discard(path: Path) -> None:
    """Delete the temp upload, tolerating a lock we cannot break.

    A malformed PDF can leave PyMuPDF holding a handle, and Windows refuses to
    delete an open file. Losing a temp file to the OS cleaner is a far smaller
    problem than turning a rejected upload into a 500.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not remove temp upload %s: %s", path, exc)


@router.post(
    "/api/v1/door-schedule/extract",
    response_model=ExtractionResult,
    tags=["extraction"],
    summary="Extract the door schedule from a construction PDF",
)
async def extract_door_schedule(
    file: UploadFile = File(..., description="Construction PDF (multipart)"),
    debug: bool = Query(False, description="Include per-page finder diagnostics"),
    allow_ai: bool = Query(True, description="Permit the vision fallback tier"),
    _key: str = Depends(require_api_key),
) -> ExtractionResult:
    started = time.perf_counter()
    async with _spooled_upload(file) as path:
        size_mb = path.stat().st_size / 1024 / 1024
        try:
            result = await in_worker(
                extract(path, allow_ai=allow_ai, debug=debug))
        except NotAPdfError as exc:
            log.warning("rejected upload: %s", exc)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="File is not a readable PDF.") from exc
        except (NoScheduleFoundError, NoRowsError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except AiUpstreamError as exc:
            # A billing or auth failure upstream must never read as "no rows
            # found". Log it too: the caller sees the detail, but whoever is
            # watching the server saw only a bare 502 with no reason at all.
            log.error("ai upstream failed: %s", exc)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                                detail=f"AI provider error: {exc}") from exc

    log.info(
        "extract file=%s size_mb=%.1f pages=%s method=%s rows=%s ms=%s",
        file.filename, size_mb, result.pages_scanned,
        result.method.value, result.row_count,
        int((time.perf_counter() - started) * 1000),
    )
    return result


@router.post(
    "/api/v1/master-sheet",
    tags=["master sheet"],
    summary="Map an already-extracted schedule onto the master door format sheet",
    response_class=Response,
    responses={200: {"content": {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {}}}},
)
async def master_sheet(
    result: ExtractionResult,
    preview: bool = Query(False, description="Return JSON rows instead of .xlsx"),
    filename: str = Query("door-schedule", description="Stem for the download"),
    _key: str = Depends(require_api_key),
) -> Response:
    """Map an extraction onto the master sheet. Takes rows, not a PDF.

    Deliberately does no extraction of its own. Taking a PDF here meant the
    document was read twice to produce one sheet: twice the wait, twice the AI
    cost, and a second chance to fail after the first read had already
    succeeded -- which is exactly what happened.

    Columns no door schedule can answer are left empty rather than guessed:
    they belong to sources that have not been loaded yet.
    """
    from app.core.master_sheet import BANDS, COLUMNS, build_rows, build_workbook

    if preview:
        # Same builder as the spreadsheet, so the screen and the download can
        # never disagree.
        rows, stats = build_rows(result)
        return JSONResponse({
            "columns": COLUMNS,
            "bands": {str(index): name for name, index in BANDS},
            "rows": rows,
            "row_count": stats.rows,
            "filled_columns": stats.filled_columns,
            "empty_columns": stats.empty_columns,
            "method": result.method.value,
            "source_pages": result.source_pages,
        })

    xlsx, stats = build_workbook(result)
    stem = Path(filename or "door-schedule").stem
    log.info("master_sheet rows=%s filled=%s of %s", stats.rows,
             len(stats.filled_columns),
             len(stats.filled_columns) + len(stats.empty_columns))

    return Response(
        content=xlsx,
        media_type=("application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet"),
        headers={
            "Content-Disposition": f'attachment; filename="{stem} - master.xlsx"',
            "X-Row-Count": str(stats.rows),
            "X-Filled-Columns": str(len(stats.filled_columns)),
            "X-Empty-Columns": ", ".join(stats.empty_columns),
        },
    )


@router.post(
    "/api/v1/door-schedule/plan-audit",
    tags=["extraction"],
    summary="Locate every scheduled door on the floor plans and report the gaps",
    response_model=PlanAudit,
)
async def plan_audit(
    file: UploadFile = File(...),
    schedule: str | None = Form(
        None,
        description="A schedule already extracted, as the JSON of an "
        "ExtractionResult (or just its `rows`). Pass it and this endpoint does "
        "not read one: no second scan of the same file, no chance of the audit "
        "disagreeing with the table on screen, and a schedule that lives in a "
        "*different* PDF can be audited against these drawings.",
    ),
    detect: bool = Query(
        False,
        description="Also find doors as shapes with the vision model, so a door "
        "with no number can be seen. This is the only part that costs money -- "
        "roughly $0.12 and 7 minutes per floor-plan sheet.",
    ),
    dry_run: bool = Query(
        False,
        description="With detect=true, report the tiles and the predicted cost "
        "and send nothing. Call this first: it is the only way to see the "
        "price before paying it.",
    ),
    budget_usd: float | None = Query(
        None, gt=0,
        description="Ceiling for this request in USD. The scan stops when it "
        "is reached and says how much of the drawing it did not read. "
        "Defaults to DETECT_BUDGET_USD.",
    ),
    _key: str = Depends(require_api_key),
) -> PlanAudit:
    """Set the schedule against the drawings.

    Without `detect` this makes no AI call at all: every door is placed from
    text the drawing already carries, so it is free and cannot invent a
    location. With `detect` the doors are found as shapes as well, which is the
    only way to see a door that carries no number.
    """
    started = time.perf_counter()
    async with _spooled_upload(file) as path:
        try:
            result = await in_worker(
                audit(path, detect=detect, dry_run=dry_run,
                      budget_usd=budget_usd, **_supplied(schedule)))
        except NotAPdfError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="File is not a readable PDF.") from exc
        except NoDoorScheduleError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except AiUpstreamError as exc:
            # Billing or auth trouble upstream must read as itself, never as
            # "no doors found".
            log.error("ai upstream failed during detection: %s", exc)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                                detail=f"AI provider error: {exc}") from exc

    log.info("plan_audit file=%s pages=%s doors=%s located=%s detected=%s "
             "cost=%s ms=%s",
             file.filename, result.pages_scanned, result.door_count,
             len(result.located), len(result.detected),
             result.scan_cost.estimated_usd if result.scan_cost else 0,
             int((time.perf_counter() - started) * 1000))
    return result


def _supplied(raw: str | None) -> dict:
    """Rows handed in by the caller, ready to pass to `audit`.

    Accepts a whole ExtractionResult or a bare list of rows, because both are
    natural things for a caller to have. Unparseable input falls back to
    re-reading the file rather than failing: a bad hint should cost time, not
    the request.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        log.warning("plan_audit: schedule was not valid JSON; reading the file")
        return {}

    if isinstance(parsed, dict):
        raw_rows, page = parsed.get("rows") or [], parsed.get("source_pages") or []
        page = page[0] if page else 0
    elif isinstance(parsed, list):
        raw_rows, page = parsed, 0
    else:
        return {}

    rows = []
    for entry in raw_rows:
        if not isinstance(entry, dict):
            continue
        try:
            rows.append(DoorRow(**entry))
        except ValidationError:
            continue
    if not rows:
        log.warning("plan_audit: schedule carried no usable rows; reading the file")
        return {}
    return {"rows": rows, "schedule_page": page}


def _parse_marks(raw: str | None) -> list[tuple[float, float, float, float, str]]:
    """Many rectangles to outline, from a JSON array of page fractions.

    Malformed entries are dropped one by one rather than failing the request:
    this is a drawing hint, and losing one door's box is a far smaller problem
    than losing the sheet it was going to be drawn on.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        log.warning("preview: marks was not valid JSON, ignoring")
        return []
    if not isinstance(parsed, list):
        return []

    out: list[tuple[float, float, float, float, str]] = []
    for entry in parsed[:_MAX_MARKS]:
        if not isinstance(entry, dict):
            continue
        try:
            x0, y0, x1, y1 = (float(entry[k]) for k in ("x0", "y0", "x1", "y1"))
        except (KeyError, TypeError, ValueError):
            continue
        if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
            continue
        out.append((x0, y0, x1, y1, str(entry.get("label", ""))[:12]))
    if len(parsed) > _MAX_MARKS:
        log.warning("preview: %d marks requested, drawing the first %d",
                    len(parsed), _MAX_MARKS)
    return out


def _parse_box(raw: str | None) -> tuple[float, float, float, float] | None:
    """'x0,y0,x1,y1' as fractions of the page, or None.

    A malformed box is ignored rather than rejected: it is a drawing hint, and
    refusing the whole preview over it would hide the page as well as the box.
    """
    if not raw:
        return None
    try:
        x0, y0, x1, y1 = (float(part) for part in raw.split(","))
    except ValueError:
        return None
    if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
        return None
    return x0, y0, x1, y1


@router.post(
    "/api/v1/door-schedule/preview",
    tags=["extraction"],
    summary="PNG of the schedule page with the detected table outlined",
    response_class=Response,
    responses={200: {"content": {"image/png": {}}}},
)
async def preview(
    file: UploadFile = File(...),
    marks: str | None = Form(
        None,
        description="JSON array of {x0,y0,x1,y1,label} in page fractions -- "
        "every door on one sheet, outlined together. Sent as a form field "
        "rather than a query parameter because a busy plan carries dozens.",
    ),
    draw: bool = Query(
        True,
        description="Draw the marks on the image. Pass false to get the same "
        "crop with nothing on it -- for a caller laying its own overlay over "
        "the plan, which can show a door's actual swing rather than a box.",
    ),
    whole: bool = Query(
        False,
        description="Render the entire sheet rather than cropping to the doors "
        "that were found. Needed to check the answer: a crop drawn around the "
        "hits cannot show a door that was missed, because it cut that part of "
        "the building away.",
    ),
    box: str | None = Query(
        None,
        description="Rectangle to outline, as 'x0,y0,x1,y1' fractions of the "
        "page. Pass the `box` from an extraction that the AI tier answered: "
        "the model returns no geometry, so without it that page has nothing "
        "to outline.",
    ),
    page: int | None = Query(None, ge=1, description="Page the box belongs to"),
    label: str | None = Query(None, description="Caption drawn on the box"),
    _key: str = Depends(require_api_key),
) -> Response:
    """Visual confirmation that the right region was located. No AI, ever.

    Never calls the model, including when drawing a box the model's output led
    to: the caller supplies that rectangle, so opening the preview twice costs
    nothing and cannot return a different answer from the extraction.
    """
    from app.core import page_finder
    from app.core.preview import render_preview
    from app.pipeline import select_fallback_pages

    rect = _parse_box(box)
    marked = _parse_marks(marks)

    async with _spooled_upload(file) as path:
      try:
        with PdfDoc(path) as doc:
            scores = page_finder.find_schedule_pages(doc)
            candidates = page_finder.passing(scores)
            by_page = {c.page: c for c in scores}
            if marked and page is not None and page in by_page:
                chosen, located = by_page[page], True
            elif rect is not None and page is not None and page in by_page:
                # The caller already knows where the table is, so neither the
                # gates nor the locator get a say -- they are what failed on
                # this page in the first place.
                chosen, located = by_page[page], True
            elif candidates:
                chosen, located = candidates[0], True
            else:
                # No recoverable geometry -- most likely a scan. Show the page
                # the vision tier would read rather than refusing outright.
                fallback = select_fallback_pages(doc, scores, doc.page_count)
                if not fallback:
                    raise HTTPException(
                        status_code=422,
                        detail=f"No door schedule found - scanned "
                               f"{doc.page_count} pages.",
                    )
                chosen, located = by_page[fallback[0]], False
            clip: list[float] = []
            drawn: list[tuple[float, float, float, float]] = []
            png = render_preview(doc, chosen, located=located, box=rect,
                                 box_label=label or "", marks=marked,
                                 clip_out=clip, drawn_out=drawn, draw=draw,
                                 whole=whole)
      except NotAPdfError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="File is not a readable PDF.") from exc

    return Response(
        content=png,
        media_type="image/png",
        headers={
            "X-Source-Page": str(chosen.page),
            "X-Table-Located": "true" if located else "false",
            "X-Box-Source": ("marks" if marked else
                             "supplied" if rect is not None else "measured"),
            "X-Mark-Count": str(len(marked)),
            # Page fractions of the region rendered, so a caller can place
            # clickable areas over the image it just received.
            "X-Clip": ",".join(f"{v:.6f}" for v in clip),
            # The rectangles as they were actually drawn, in the order the
            # marks were sent. A small door's box is grown to stay visible, so
            # these are not the marks that went in -- and an overlay built from
            # those would not sit on the boxes a person can see.
            "X-Drawn": ";".join(
                ",".join(f"{v:.6f}" for v in box) for box in drawn),
            "Access-Control-Expose-Headers":
                "X-Source-Page, X-Table-Located, X-Box-Source, X-Mark-Count, "
                "X-Clip, X-Drawn",
        },
    )


@router.post("/api/v1/door-schedule/inspect", tags=["extraction"],
             summary="Page-finder diagnostics only -- no extraction, no AI")
async def inspect(
    file: UploadFile = File(...),
    _key: str = Depends(require_api_key),
) -> dict:
    """Scores every page. Use this to retune thresholds against new bid sets."""
    from app.core import page_finder

    started = time.perf_counter()
    async with _spooled_upload(file) as path:
        size_mb = path.stat().st_size / 1024 / 1024
        try:
            with PdfDoc(path) as doc:
                scores = page_finder.find_schedule_pages(doc)
                pages = doc.page_count
        except NotAPdfError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="File is not a readable PDF.") from exc

    return {
        "filename": file.filename,
        "pages_scanned": pages,
        "size_mb": round(size_mb, 2),
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "passing_pages": [c.page for c in page_finder.passing(scores)],
        "scores": [c.as_dict() for c in sorted(scores, key=lambda c: c.score, reverse=True)
                   if c.score > 0],
    }

