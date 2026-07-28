from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status

from app import __version__
from app.ai.client import AiUpstreamError
from app.api.deps import require_api_key
from app.config import get_settings
from app.core.pdf_doc import NotAPdfError, PdfDoc
from app.pipeline import NoRowsError, NoScheduleFoundError, extract
from app.schemas import ExtractionResult, HealthResponse

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health", response_model=HealthResponse, tags=["meta"])
async def health() -> HealthResponse:
    return HealthResponse(version=__version__, ai_enabled=get_settings().ai_enabled)


def _too_large(actual_mb: float | None, limit_mb: float) -> HTTPException:
    size = f"{actual_mb:.1f} MB" if actual_mb is not None else f"over {limit_mb:.0f} MB"
    return HTTPException(
        status_code=413,
        detail=f"PDF too large ({size}). Maximum is {limit_mb:.0f} MB.",
    )


async def _read_upload(file: UploadFile) -> bytes:
    settings = get_settings()
    limit = int(settings.max_upload_mb * 1024 * 1024)

    # Starlette knows the part size up front; reject before buffering anything.
    declared = getattr(file, "size", None)
    if declared is not None and declared > limit:
        raise _too_large(declared / 1024 / 1024, settings.max_upload_mb)

    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(1024 * 1024):
        total += len(chunk)
        if total > limit:
            # Read no further -- the point of the cap is to not hold the file.
            raise _too_large(None, settings.max_upload_mb)
        chunks.append(chunk)
    if not total:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="File is empty.")
    return b"".join(chunks)


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
    data = await _read_upload(file)

    try:
        result = await extract(data, allow_ai=allow_ai, debug=debug)
    except NotAPdfError as exc:
        log.warning("rejected upload: %s", exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="File is not a readable PDF.") from exc
    except NoScheduleFoundError as exc:
        raise HTTPException(status_code=422,
                            detail=str(exc)) from exc
    except NoRowsError as exc:
        raise HTTPException(status_code=422,
                            detail=str(exc)) from exc
    except AiUpstreamError as exc:
        # A billing or auth failure upstream must never read as "no rows found".
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"AI provider error: {exc}") from exc

    log.info(
        "extract file=%s size_mb=%.1f pages=%s method=%s rows=%s ms=%s",
        file.filename, len(data) / 1024 / 1024, result.pages_scanned,
        result.method.value, result.row_count,
        int((time.perf_counter() - started) * 1000),
    )
    return result


@router.post("/api/v1/door-schedule/inspect", tags=["extraction"],
             summary="Page-finder diagnostics only -- no extraction, no AI")
async def inspect(
    file: UploadFile = File(...),
    _key: str = Depends(require_api_key),
) -> dict:
    """Scores every page. Use this to retune thresholds against new bid sets."""
    from app.core import page_finder

    data = await _read_upload(file)
    started = time.perf_counter()
    try:
        with PdfDoc(data) as doc:
            scores = page_finder.find_schedule_pages(doc)
            pages = doc.page_count
    except NotAPdfError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="File is not a readable PDF.") from exc

    return {
        "filename": file.filename,
        "pages_scanned": pages,
        "size_mb": round(len(data) / 1024 / 1024, 2),
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "passing_pages": [c.page for c in page_finder.passing(scores)],
        "scores": [c.as_dict() for c in sorted(scores, key=lambda c: c.score, reverse=True)
                   if c.score > 0],
    }

