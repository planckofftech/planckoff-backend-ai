"""Tier orchestration.

Try each tier in order, record which one won, and never let a failed tier kill
the request -- a failure becomes a warning and the next tier runs. Only an
all-tiers-failed state is an error.
"""

from __future__ import annotations

import logging
import time

from app.ai.client import AiUpstreamError
from app.config import get_settings
from app.core import page_finder
from app.core.extractor import extract_pages
from app.core.pdf_doc import PdfDoc
from app.schemas import ExtractionMethod, ExtractionResult, PageScore

log = logging.getLogger(__name__)

# Below this page count it is cheap enough to let the AI look at the best-
# scoring page even though no page passed the structural gates.
_SMALL_DOC_PAGES = 20


class NoScheduleFoundError(RuntimeError):
    def __init__(self, pages_scanned: int):
        self.pages_scanned = pages_scanned
        super().__init__(
            f"No door schedule found - scanned {pages_scanned} pages. "
            "Verify this document contains a door schedule sheet."
        )


class NoRowsError(RuntimeError):
    def __init__(self, pages: list[int], pages_scanned: int):
        self.pages = pages
        self.pages_scanned = pages_scanned
        page_list = ", ".join(str(p) for p in pages)
        super().__init__(
            f"Found a door schedule on page {page_list} but could not read any rows."
        )


async def extract(pdf_bytes: bytes, *, allow_ai: bool = True,
                  debug: bool = False) -> ExtractionResult:
    """PDF in, JSON out. Stateless.

    Kept async so swapping in a job queue later means changing the route, not
    the logic.
    """
    settings = get_settings()
    started = time.perf_counter()
    warnings: list[str] = []

    with PdfDoc(pdf_bytes) as doc:
        pages_scanned = doc.page_count

        scores = page_finder.find_schedule_pages(
            doc,
            min_header_hits=settings.min_header_hits,
            min_tag_run=settings.min_tag_run,
        )
        candidates = page_finder.passing(scores)
        log.info("page_finder pages=%s candidates=%s best=%s",
                 pages_scanned, [c.page for c in candidates],
                 max((c.score for c in scores), default=0))

        page_scores = [PageScore(**c.as_dict()) for c in scores] if debug else []

        # --- Tier 1: deterministic ------------------------------------------
        best = None
        if candidates:
            for extraction in extract_pages(doc, candidates):
                warnings.extend(extraction.warnings)
                if extraction.rows and (best is None or len(extraction.rows) > len(best.rows)):
                    best = extraction

        if best is not None and best.rows:
            duration = int((time.perf_counter() - started) * 1000)
            log.info("extracted method=%s page=%s rows=%s ms=%s",
                     best.method.value, best.page, len(best.rows), duration)
            return ExtractionResult(
                method=best.method,
                pages_scanned=pages_scanned,
                source_pages=[best.page],
                row_count=len(best.rows),
                duration_ms=duration,
                warnings=warnings,
                headers=best.headers,
                rows=best.rows,
                page_scores=page_scores,
            )

        # --- Tier 2: AI vision, candidate pages only -------------------------
        ai_pages = [c.page for c in candidates[:2]]

        # Nothing passed the gates. On a small document the best-scoring page is
        # still worth a look, but that is a guess, not a find -- and it must not
        # be reported as one. `speculative` keeps the two apart so the error
        # message never claims a schedule was located when it was not.
        speculative = False
        if not ai_pages and pages_scanned <= _SMALL_DOC_PAGES:
            ranked = sorted(scores, key=lambda c: c.score, reverse=True)
            ai_pages = [c.page for c in ranked[:1] if c.score > 0]
            speculative = bool(ai_pages)

        def _give_up() -> RuntimeError:
            if speculative or not candidates:
                return NoScheduleFoundError(pages_scanned)
            return NoRowsError([c.page for c in candidates], pages_scanned)

        if not ai_pages:
            raise NoScheduleFoundError(pages_scanned)

        if not allow_ai:
            warnings.append("AI fallback disabled for this request")
            raise _give_up()

        if not settings.ai_enabled:
            warnings.append(
                "AI fallback unavailable: OPENROUTER_API_KEY is not configured"
            )
            raise _give_up()

        from app.ai.vision_extract import extract_with_vision

        for page in ai_pages:
            try:
                rows, ai_warnings = await extract_with_vision(doc, page)
            except AiUpstreamError:
                raise
            except Exception as exc:  # noqa: BLE001 - a tier failure is a warning
                warnings.append(f"AI fallback failed on page {page}: {exc}")
                continue
            warnings.extend(ai_warnings)
            if rows:
                duration = int((time.perf_counter() - started) * 1000)
                log.info("extracted method=ai_vision page=%s rows=%s ms=%s",
                         page, len(rows), duration)
                return ExtractionResult(
                    method=ExtractionMethod.AI_VISION,
                    pages_scanned=pages_scanned,
                    source_pages=[page],
                    row_count=len(rows),
                    duration_ms=duration,
                    warnings=warnings,
                    rows=rows,
                    page_scores=page_scores,
                )

        raise _give_up()
