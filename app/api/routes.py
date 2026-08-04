from __future__ import annotations

import logging
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import JSONResponse

from app import __version__
from app.ai.client import AiUpstreamError
from app.api.deps import require_api_key
from app.config import get_settings
from app.core.pdf_doc import NotAPdfError, PdfDoc
from app.pipeline import NoRowsError, NoScheduleFoundError, extract
from app.schemas import ExtractionResult, HealthResponse

log = logging.getLogger(__name__)
router = APIRouter()

_UPLOAD_CHUNK = 1024 * 1024


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
            result = await extract(path, allow_ai=allow_ai, debug=debug)
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

    async with _spooled_upload(file) as path:
      try:
        with PdfDoc(path) as doc:
            scores = page_finder.find_schedule_pages(doc)
            candidates = page_finder.passing(scores)
            by_page = {c.page: c for c in scores}
            if rect is not None and page is not None and page in by_page:
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
            png = render_preview(doc, chosen, located=located, box=rect,
                                 box_label=label or "")
      except NotAPdfError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="File is not a readable PDF.") from exc

    return Response(
        content=png,
        media_type="image/png",
        headers={
            "X-Source-Page": str(chosen.page),
            "X-Table-Located": "true" if located else "false",
            "X-Box-Source": "supplied" if rect is not None else "measured",
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

