"""Deterministic extraction of one candidate page: grid -> cells -> DoorRows."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.core import cell_mapper, header_mapper, page_finder, row_builder
from app.core.page_finder import PageCandidate
from app.core.pdf_doc import PdfDoc
from app.core.table_locator import TableNotFoundError, locate_table, table_title
from app.schemas import DoorRow, ExtractionMethod

log = logging.getLogger(__name__)


# Tag-column length required of a *further* table on a page that already
# qualified. The page-level gate stays at its full strength.
_SECONDARY_TAG_RUN = 3

# How much of a block has to make sense before we call it a schedule.
_MIN_MAPPED_FIELDS = 4
_MIN_FIELDS_WITH_TAG = 3
_MIN_SCHEDULE_ROWS = 2

# A sheet carries other schedules that share a door schedule's vocabulary --
# WIDTH, HEIGHT, TYPE, MATERIAL, FINISH, HEAD, JAMB -- so keyword scoring alone
# cannot tell them apart. The printed caption can.
_DOOR_WORDS = ("DOOR", "OPENING", "FRAME", "LEAF")
_OTHER_SCHEDULES = ("WINDOW", "GLAZING", "GLASS", "ROOM FINISH", "FINISH SCHEDULE",
                    "LOUVER", "STOREFRONT", "CASEWORK", "PARTITION", "WALL TYPE",
                    "EQUIPMENT", "LIGHTING", "PLUMBING", "SIGNAGE",
                    # A toilet-accessories block is tagged TA1, TA2, TA3 -- which
                    # is tag-shaped, distinct and sequential, so nothing about
                    # its data says "not doors". Its caption does.
                    "ACCESSOR", "SPECIALT", "APPLIANCE", "MILLWORK")


def _names_doors(rows: list[DoorRow]) -> bool:
    """Does the tag column actually hold door numbers?

    A hardware-group list prints the door it belongs to as a heading -- "DOOR
    #: X7.101" -- and its QTY column of 1s and 2s is tag-shaped, which was
    enough to let a catalogue of hinges and closers through as a 34-row
    schedule beside the real 14-row one.

    So: tag-shaped, and mostly distinct. Numbering doors is what a door
    schedule is for, and a column that repeats the same few values is counting
    something instead.
    """
    tags = [r.door_tag for r in rows if r.door_tag]
    if len(tags) < _MIN_SCHEDULE_ROWS:
        return False
    if len(set(tags)) * 2 < len(tags):
        return False
    return sum(bool(page_finder.TAG_RE.match(t)) for t in tags) * 2 >= len(tags)


def looks_like_a_schedule(mapped: list[str | None], headers: list[str],
                          rows: list[DoorRow]) -> bool:
    """Does this block behave like a door schedule, or merely sit in a box?

    Searching a sheet for *every* table finds the extra door schedules it was
    meant to find, and also finds hardware group lists, finish legends and
    drawing notes -- anything ruled. One sheet returned seven "schedules" and
    181 rows, of which one was real.

    A title cannot settle it, because these blocks have no title. What settles
    it is how much of the block we could understand: a real schedule names
    several door fields, junk names one and fills the rest with Column 24.
    """
    fields = {f for f in mapped if f}
    if len(rows) < _MIN_SCHEDULE_ROWS:
        return False
    if len(fields) >= _MIN_MAPPED_FIELDS:
        return True
    # A genuine door tag is worth a lot: with one, fewer other fields will do.
    # It has to be a genuine one, though -- see _names_doors.
    return ("door_tag" in fields and len(fields) >= _MIN_FIELDS_WITH_TAG
            and _names_doors(rows))


def is_other_schedule(title: str) -> bool:
    """True when the caption names a schedule that is plainly not about doors.

    An untitled table is kept: it cannot be proven innocent or guilty, and
    dropping untitled tables would lose sheets whose door schedule has no
    caption of its own.
    """
    text = re.sub(r"\s+", " ", (title or "").upper())
    if not text:
        return False
    if any(word in text for word in _DOOR_WORDS):
        return False
    return any(word in text for word in _OTHER_SCHEDULES)


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
    # Canonical field per column, aligned to `headers`; None where unmapped.
    mapped: list[str | None] = field(default_factory=list)


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

    columns = [[r[i] if i < len(r) else "" for r in raw_rows]
               for i in range(len(header_strings))]
    before = list(mapped)
    mapped = header_mapper.infer_tag_column(mapped, columns, header_strings)
    if mapped != before:
        claimed = header_strings[mapped.index("door_tag")]
        warnings.append(
            f"no column named the door tag; read it from the data in {claimed!r}"
        )
        unmapped = [h for h in unmapped if h != claimed]
    tag_col = header_mapper.tag_column_index(mapped)

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
                          unmapped, candidate, title, mapped)


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
    skipped: list[PageExtraction] = []
    rejected: list[PageExtraction] = []
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
                extraction = extract_page(doc, band)
            except (TableNotFoundError, IndexError, ValueError) as exc:
                out.append(PageExtraction(
                    band.page, ExtractionMethod.NONE, [], [],
                    [f"page {band.page}: a table at y={band.header_y:.0f} "
                     f"could not be read ({exc})"],
                ))
                continue

            if is_other_schedule(extraction.title):
                log.info("skipping %r on page %s (%s rows): another schedule",
                         extraction.title, extraction.page, len(extraction.rows))
                skipped.append(extraction)
                continue

            if not looks_like_a_schedule(extraction.mapped, extraction.headers,
                                         extraction.rows):
                log.info("skipping block on page %s at y=%.0f (%s rows, %s fields "
                         "understood): does not behave like a schedule",
                         extraction.page, band.header_y, len(extraction.rows),
                         len({f for f in extraction.mapped if f}))
                rejected.append(extraction)
                continue
            out.append(extraction)

    # The test above is meant to remove junk that sits *alongside* a real
    # schedule. When it rejects everything, the page still passed the full
    # structural gates, so the strongest block is more likely a schedule we
    # mapped badly than a legend -- return it rather than nothing, and say so.
    if not any(e.rows for e in out) and rejected:
        best = max(rejected, key=lambda e: (len({f for f in e.mapped if f}),
                                            len(e.rows)))
        best.warnings.append(
            "only a few columns on this table could be identified; check the "
            "rows against the drawing"
        )
        out.append(best)

    # Only worth mentioning when it is the difference between rows and nothing:
    # otherwise it is noise on every multi-schedule sheet.
    if skipped and not any(e.rows for e in out):
        names = ", ".join(f"{e.title} ({len(e.rows)} rows)" for e in skipped)
        out.append(PageExtraction(
            skipped[0].page, ExtractionMethod.NONE, [], [],
            [f"Found no door schedule. Other schedules on this sheet: {names}."],
        ))
    return out




