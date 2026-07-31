"""Deterministic extraction of one candidate page: grid -> cells -> DoorRows."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core import cell_mapper, header_mapper, page_finder, row_builder
from app.core.page_finder import PageCandidate
from app.core.pdf_doc import PdfDoc
from app.core.table_locator import TableNotFoundError, locate_table, table_title
from app.schemas import DoorRow, ExtractionMethod


# Tag-column length required of a *further* table on a page that already
# qualified. The page-level gate stays at its full strength.
_SECONDARY_TAG_RUN = 3


@dataclass(slots=True)
class PageExtraction:
    page: int
    method: ExtractionMethod
    headers: list[str]
    rows: list[DoorRow]
    warnings: list[str]
    unmapped: list[str] = field(default_factory=list)
    candidate: PageCandidate | None = None
    title: str = ""


def _is_noise(cells: list[str], tag_col: int) -> bool:
    """Drop grid artefacts that survived the row walk.

    A row of nothing but the tag column, or a row whose every cell is a single
    punctuation mark, carries no schedule content.
    """
    populated = [c for c in cells if c]
    if not populated:
        return True
    if len(populated) == 1 and cells[tag_col] and len(cells[tag_col]) <= 2:
        return True
    return all(len(c) <= 1 and not c.isalnum() for c in populated)


def extract_page(doc: PdfDoc, candidate: PageCandidate,
                 header_overrides: dict[str, str] | None = None) -> PageExtraction:
    page_index = candidate.page - 1
    items = doc.text_items(page_index)
    rulings = doc.rulings(page_index)

    grid, headers = locate_table(items, rulings, candidate.header_y, candidate.tag_x)
    warnings = list(grid.warnings)

    _title_top, title = table_title(grid, items, rulings)
    header_strings = cell_mapper.header_texts(grid, headers, items, rulings)
    mapped, unmapped = header_mapper.map_headers(header_strings, header_overrides)
    tag_col = header_mapper.tag_column_index(mapped)

    if grid.mode == "ruled":
        raw_rows = cell_mapper.cells_by_ruled_rows(grid, items)
        method = ExtractionMethod.DETERMINISTIC_RULED
    else:
        raw_rows = row_builder.build_rows(grid, items, tag_col)
        method = ExtractionMethod.DETERMINISTIC_BANDED

    # The header line itself is a row of the ruled grid; drop it.
    if raw_rows and _looks_like_header(raw_rows[0], header_strings):
        raw_rows = raw_rows[1:]

    if unmapped:
        warnings.append(
            f"unmapped column headers kept under 'extra': {', '.join(unmapped)}"
        )

    rows: list[DoorRow] = []
    for cells in raw_rows:
        if _is_noise(cells, tag_col):
            continue
        values: dict[str, str] = {}
        extra: dict[str, str] = {}
        for idx, text in enumerate(cells):
            if idx >= len(mapped):
                continue
            field = mapped[idx]
            if field:
                values[field] = text
            elif text:
                extra[header_mapper.extra_key(header_strings[idx], idx)] = text
        rows.append(DoorRow(**values, extra=extra))

    return PageExtraction(candidate.page, method, header_strings, rows, warnings,
                          unmapped, candidate, title)


def _looks_like_header(cells: list[str], header_strings: list[str]) -> bool:
    norm_row = {header_mapper.normalize(c) for c in cells if c}
    norm_hdr = {header_mapper.normalize(h) for h in header_strings if h}
    if not norm_row or not norm_hdr:
        return False
    return len(norm_row & norm_hdr) >= max(2, len(norm_row) // 2)


def extract_pages(doc: PdfDoc, candidates: list[PageCandidate]) -> list[PageExtraction]:
    """Every schedule on every candidate page. A page that throws is recorded,
    not fatal.

    One sheet often stacks several schedules, so each candidate page is searched
    for all of its header rows rather than just the strongest.
    """
    out: list[PageExtraction] = []
    seen: set[tuple[int, int]] = set()

    for candidate in candidates:
        # This page already passed the structural gates, so the expensive
        # question -- is this a schedule sheet? -- is settled. Additional
        # tables on it can be admitted on a lower bar; a guestroom schedule
        # with seven rows is still a schedule.
        bands = page_finder.header_bands(
            doc.text_items(candidate.page - 1), candidate.page,
            min_tag_run=_SECONDARY_TAG_RUN,
        ) or [candidate]
        for band in bands:
            key = (band.page, int(round(band.header_y)))
            if key in seen:
                continue
            seen.add(key)
            try:
                out.append(extract_page(doc, band))
            except (TableNotFoundError, IndexError, ValueError) as exc:
                out.append(PageExtraction(
                    band.page, ExtractionMethod.NONE, [], [],
                    [f"page {band.page}: a table at y={band.header_y:.0f} "
                     f"could not be read ({exc})"],
                ))
    return out
