"""Tier orchestration.

Try each tier in order, record which one won, and never let a failed tier kill
the request -- a failure becomes a warning and the next tier runs. Only an
all-tiers-failed state is an error.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from app.ai.client import AiUpstreamError
from app.config import get_settings
from app.core import header_mapper, page_finder
from app.core.extractor import PageExtraction, extract_page, extract_pages
from app.core.pdf_doc import PdfDoc
from app.schemas import (
    ExtractionMethod,
    ExtractionResult,
    PageScore,
    ScheduleTable,
)

log = logging.getLogger(__name__)

# Below this page count it is cheap enough to let the AI look at the best-
# scoring page even though no page passed the structural gates.
_SMALL_DOC_PAGES = 20
# Below this many unplaced columns the alias table is doing fine on its own.
_AI_HEADER_THRESHOLD = 2
# A scan longer than this is worth mentioning in the response.
_SLOW_SCAN_MS = 15_000
# Below either of these, a deterministic read is treated as a partial one and
# the vision tier is asked for a second opinion.
_MIN_REAL_COLUMNS = 5
_MIN_FILL_RATIO = 0.30
# Never render more than this many pages to the model, whatever the page count.
_MAX_AI_PAGES = 2


def select_fallback_pages(doc: PdfDoc, scores: list, pages_scanned: int) -> list[int]:
    """Pages worth handing to the vision tier when nothing passed the gates.

    A scanned sheet has no text to score, so it can never pass -- it is exactly
    what the vision tier is for, and that must not be gated on document size.
    Shared with the preview endpoint so both agree on which page matters.
    """
    scanned = [c for c in scores
               if not c.has_text_layer and doc.has_raster(c.page - 1)]

    shortlist = {c.page: c for c in scanned}
    if scanned or pages_scanned <= _SMALL_DOC_PAGES:
        # Keep any page that scored at all. A scan often retains a thin text
        # layer on the sheet that matters, and that page is a far better bet
        # than the first bitmap in the file.
        shortlist.update({c.page: c for c in scores if c.score > 0})

    if not shortlist and pages_scanned <= _SMALL_DOC_PAGES:
        # Nothing scored anywhere. On a short document that means the sheet is
        # a picture carrying a title block's worth of text -- just over the
        # count that marks a page "scanned", far under anything readable. One
        # such sheet had 40 text items, none of them a heading, and was
        # reported as having no schedule without the model ever seeing it.
        shortlist.update({c.page: c for c in scores
                          if doc.has_raster(c.page - 1)})

    ranked = sorted(shortlist.values(), key=lambda c: (-c.score, c.page))
    return [c.page for c in ranked[:_MAX_AI_PAGES]]


def _is_thin(extraction: PageExtraction) -> str | None:
    """Why this deterministic read looks incomplete, or None if it looks fine.

    Sheets are routinely half text and half picture: the header prints as real
    text, the rows do not. We then find the header, decide the page is
    readable, and return the two or three columns that happen to exist -- which
    looks like a complete answer and is not. One sheet returned 52 rows of 3
    columns from a table with twenty.
    """
    columns = len(extraction.headers)
    if columns < _MIN_REAL_COLUMNS:
        return f"only {columns} columns were readable as text"

    cells = filled = 0
    for row in extraction.rows:
        values = row.model_dump()
        extra = values.pop("extra", {})
        cells += columns
        filled += sum(1 for v in list(values.values()) + list(extra.values())
                      if isinstance(v, str) and v)
    if cells and filled / cells < _MIN_FILL_RATIO:
        return f"only {100 * filled / cells:.0f}% of cells held any text"
    return None


def _column_samples(extractions: list[PageExtraction], headers: list[str],
                    limit: int = 3) -> dict[str, list[str]]:
    """A few example values per unplaced column, for the header mapper.

    A heading alone cannot say whether a column identifies the row or
    classifies it -- which is how door numbers ended up in door_type.
    """
    samples: dict[str, list[str]] = {header: [] for header in headers}
    wanted = set(headers)

    for extraction in extractions:
        columns = [
            index for index, name in enumerate(extraction.headers)
            if name in wanted
        ]
        for row in extraction.rows:
            values = list(row.extra.values())
            for index in columns:
                header = extraction.headers[index]
                if len(samples[header]) >= limit:
                    continue
                key = header_mapper.extra_key(header, index)
                value = row.extra.get(key) or ""
                if value and value not in samples[header]:
                    samples[header].append(value)
            _ = values

    return {k: v for k, v in samples.items() if v}


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


async def extract(source: bytes | str | Path, *, allow_ai: bool = True,
                  debug: bool = False) -> ExtractionResult:
    """PDF in, JSON out. Stateless.

    `source` is bytes or a path. A path keeps memory flat on a large set --
    see PdfDoc.

    Kept async so swapping in a job queue later means changing the route, not
    the logic.
    """
    settings = get_settings()
    started = time.perf_counter()
    warnings: list[str] = []

    with PdfDoc(source) as doc:
        pages_scanned = doc.page_count

        scores = page_finder.find_schedule_pages(
            doc,
            min_header_hits=settings.min_header_hits,
            min_tag_run=settings.min_tag_run,
        )
        candidates = page_finder.passing(scores)
        scan_ms = int((time.perf_counter() - started) * 1000)
        log.info("page_finder pages=%s candidates=%s best=%s ms=%s",
                 pages_scanned, [c.page for c in candidates],
                 max((c.score for c in scores), default=0), scan_ms)

        # A large set is slow whatever we do. Say so, so a long wait is visible
        # before a demo rather than during one.
        if scan_ms > _SLOW_SCAN_MS:
            warnings.append(
                f"Scanning {pages_scanned} pages took {scan_ms / 1000:.0f} s. "
                "Large drawing sets are slow to read; this is not a fault."
            )

        page_scores = [PageScore(**c.as_dict()) for c in scores] if debug else []

        # --- Tier 1: deterministic ------------------------------------------
        best = None
        found: list[PageExtraction] = []
        if candidates:
            extractions = extract_pages(doc, candidates)
            found = [e for e in extractions if e.rows]
            for extraction in found:
                if best is None or len(extraction.rows) > len(best.rows):
                    best = extraction
            for extraction in extractions:
                if not extraction.rows:
                    warnings.extend(extraction.warnings)

        if best is not None and best.rows:
            # Columns the alias table could not place. No firm names them the
            # same way, so resolve the leftovers instead of shipping them as
            # extras -- headers only, never the table. Pooled across every
            # schedule on the sheet: they share a vocabulary, so one call
            # answers for all of them, and resolving only the largest left the
            # rest with the same columns filed under `extra`.
            leftovers: list[str] = []
            for extraction in found:
                for header in extraction.unmapped:
                    if header not in leftovers:
                        leftovers.append(header)

            if (len(leftovers) >= _AI_HEADER_THRESHOLD and allow_ai
                    and settings.ai_enabled):
                from app.ai.header_map import resolve_headers

                samples = _column_samples(found, leftovers)
                overrides, hint_warnings = await resolve_headers(leftovers, samples)
                warnings.extend(hint_warnings)
                if overrides:
                    refreshed = []
                    for extraction in found:
                        if extraction.unmapped and extraction.candidate is not None:
                            retried = extract_page(doc, extraction.candidate,
                                                   overrides)
                            refreshed.append(retried if retried.rows else extraction)
                        else:
                            refreshed.append(extraction)
                    found = refreshed
                    best = max(found, key=lambda e: len(e.rows))
            warnings.extend(best.warnings)

            # A partial read looks like a complete answer, so check before
            # trusting it. The vision tier only wins if it actually understands
            # more of the table -- otherwise the measured read stands.
            thin = _is_thin(best)
            if thin and allow_ai and settings.ai_enabled:
                warnings.append(
                    f"the text layer on page {best.page} looks incomplete "
                    f"({thin}); re-reading the page as an image"
                )
                from app.ai.vision_extract import extract_with_vision

                try:
                    ai_rows, ai_headers, ai_warnings = await extract_with_vision(
                        doc, best.page)
                except AiUpstreamError:
                    raise
                except Exception as exc:  # noqa: BLE001 - a tier failure warns
                    ai_rows, ai_headers, ai_warnings = [], [], [
                        f"image re-read failed: {exc}"]

                if ai_rows and len(ai_headers) > len(best.headers):
                    warnings.extend(ai_warnings)
                    duration = int((time.perf_counter() - started) * 1000)
                    log.info("extracted method=ai_vision page=%s rows=%s ms=%s "
                             "(deterministic read was thin: %s)",
                             best.page, len(ai_rows), duration, thin)
                    return ExtractionResult(
                        method=ExtractionMethod.AI_VISION,
                        pages_scanned=pages_scanned,
                        source_pages=[best.page],
                        row_count=len(ai_rows),
                        duration_ms=duration,
                        warnings=warnings,
                        headers=ai_headers,
                        rows=ai_rows,
                        tables=[ScheduleTable(
                            title=best.title, page=best.page, headers=ai_headers,
                            row_count=len(ai_rows), rows=ai_rows)],
                        page_scores=page_scores,
                    )
                warnings.append(
                    "the image re-read did not recover more columns; "
                    "keeping the text-layer result"
                )

            tables = [
                ScheduleTable(title=e.title, page=e.page, headers=e.headers,
                              field_map=e.mapped, row_count=len(e.rows), rows=e.rows)
                for e in sorted(found, key=lambda e: (e.page, -len(e.rows)))
            ]
            if len(tables) > 1:
                warnings.append(
                    f"{len(tables)} schedules found on this sheet; "
                    f"'rows' holds the largest"
                )
            duration = int((time.perf_counter() - started) * 1000)
            log.info("extracted method=%s page=%s rows=%s ms=%s",
                     best.method.value, best.page, len(best.rows), duration)
            return ExtractionResult(
                method=best.method,
                pages_scanned=pages_scanned,
                source_pages=sorted({t.page for t in tables}),
                row_count=len(best.rows),
                duration_ms=duration,
                warnings=warnings,
                headers=best.headers,
                rows=best.rows,
                tables=tables,
                page_scores=page_scores,
            )

        # --- Tier 2: AI vision, candidate pages only -------------------------
        ai_pages = [c.page for c in candidates[:2]]

        # Nothing passed the gates. On a small document the best-scoring page is
        # still worth a look, but that is a guess, not a find -- and it must not
        # be reported as one. `speculative` keeps the two apart so the error
        # message never claims a schedule was located when it was not.
        speculative = False
        if not ai_pages:
            scanned = [c for c in scores
                       if not c.has_text_layer and doc.has_raster(c.page - 1)]
            ai_pages = select_fallback_pages(doc, scores, pages_scanned)
            speculative = bool(ai_pages) and not scanned

            if scanned and ai_pages:
                warnings.append(
                    f"{len(scanned)} page(s) have no text layer; "
                    f"reading page(s) {ai_pages} as images"
                )

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
                rows, ai_headers, ai_warnings, box = await extract_with_vision(
                    doc, page)
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
                    headers=ai_headers,
                    rows=rows,
                    page_scores=page_scores,
                    box=box,
                )

        raise _give_up()

