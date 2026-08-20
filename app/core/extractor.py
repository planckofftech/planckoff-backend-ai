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
                    "ACCESSOR", "SPECIALT", "APPLIANCE", "MILLWORK",
                    # Mechanical and electrical schedules share a door
                    # schedule's whole vocabulary -- MARK, TYPE, MATERIAL,
                    # FINISH -- so they clear every structural test there is.
                    # One 187-page set returned its door schedule and three
                    # AIR DEVICE SCHEDULEs, 39 diffusers priced as doors.
                    "AIR DEVICE", "AIR TERMINAL", "DIFFUSER", "GRILLE",
                    "REGISTER", "MECHANICAL", "ELECTRICAL", "PANELBOARD",
                    "LUMINAIRE", "FIXTURE", "VAV", "RTU", "FAN ", "PUMP")


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
                values[field] = _join_split_mark(text) if field == "door_tag" \
                    else text
            elif text:
                extra[header_mapper.extra_key(header_strings[idx], idx)] = text
        # A second line of the heading, wearing a door's clothes.
        if _is_heading_row(values.get("door_tag", "")):
            continue
        _split_run_on_tag(values)
        rows.append(DoorRow(**values, extra=extra))

    # A mark carrying a character no font could have meant. One sheet's embedded
    # font has a damaged ToUnicode map -- every glyph it gets wrong comes back
    # 29 code points low, so TYPE reads "T<PE" and door 106 reads "10" plus a
    # control character. Left unsaid, that door is quietly unmatchable: its
    # number is not what the plan prints, and stripping the bad character would
    # turn 106 and 108 into two doors both called 10.
    damaged = [r.door_tag for r in rows
               if any(ord(c) < 32 for c in r.door_tag)]
    if damaged:
        warnings.append(
            f"{len(damaged)} door number(s) could not be read: this sheet's "
            "embedded font has a damaged character map. Check them against the "
            "drawing before pricing -- they will not match the floor plan."
        )

    return PageExtraction(candidate.page, method, header_strings, rows, warnings,
                          unmapped, candidate, title, mapped)


# A door mark printed as a number and a letter with air between them. Real
# schedules do this: the number is set under the room-number column rule and the
# suffix a little to its right, so the text layer gives back "105 B" for what
# the drawing calls 105B and what is stencilled 105B on the plan.
#
# One letter, or two. Never more: "101 HM" is a mark and a material that have
# run together, and gluing those would invent a door.
_SPLIT_MARK = re.compile(r"^(\d{1,4})\s+([A-Za-z]{1,2})$")


# A door number with a room name stuck to it. Where a column boundary falls
# slightly wrong the neighbouring cell's text lands in the number's cell, and
# "114 ALTERATIOM ROOM" is then the door's name everywhere downstream -- it
# matches nothing on the plan and is reported as a door in the schedule that
# was never drawn.
_TRAILING_WORDS = re.compile(r"^(\S+)\s+(.*[A-Za-z]{3}.*)$")


def _split_run_on_tag(values: dict[str, str]) -> None:
    """Separate a door number from the room name that ran into its cell.

    Where a column boundary falls slightly wrong the neighbouring cell's text
    lands in the number's: "114 ALTERATIOM ROOM" becomes the door's name
    everywhere downstream, matches nothing on the plan, and is reported as a
    scheduled door that was never drawn.

    The words are *moved*, not dropped. They are the room this door comes from,
    and that cell is empty precisely because its text ended up here -- so
    trimming the tag and stopping would fix the number by losing the location.

    Only ever splits when the first token is tag-shaped on its own and what
    follows holds a real word. "105 B" is a mark and is joined before this runs.
    """
    tag = values.get("door_tag", "")
    found = _TRAILING_WORDS.match(tag.strip())
    if not found or not page_finder.TAG_RE.match(found.group(1)):
        return
    values["door_tag"] = found.group(1)
    if not values.get("from_space"):
        values["from_space"] = found.group(2).strip()


# A heading that survived into the rows. The first line is dropped by matching
# it against the joined header text, but a schedule with a stacked heading has a
# second line -- "DOOR NO. | W | SIZE HT | TYPE | MATL" -- that matches nothing
# and becomes a door. It is then reported as scheduled and never drawn, which is
# the most expensive wrong answer this service gives.
_HEADING_TAGS = frozenset(
    header_mapper.normalize(a)
    for field, aliases in header_mapper.HEADER_ALIASES if field == "door_tag"
    for a in aliases
) | {"DOOR", "MARK", "NO", "DOOR NO", "NUMBER", "TAG", "SYMBOL"}


def _is_heading_row(tag: str) -> bool:
    """Is this row's "door number" a label rather than a door?

    Two kinds, and both were being priced as doors. A stacked heading's second
    line lands in the rows as "DOOR NO."; and a schedule that groups its doors
    by storey prints "FIRST FLOOR" across the number column as a band, which
    came back four times over on one set.

    A door number always carries a digit -- that is what `TAG_RE` is built on
    and what the whole matcher relies on -- so a run of letters in the number
    column is a caption, whatever it says.
    """
    text = header_mapper.normalize(tag)
    if not text:
        return False
    if text in _HEADING_TAGS:
        return True
    return not any(ch.isdigit() for ch in text) and len(text) >= _MIN_LABEL_LEN


# Shorter than this and a letters-only mark could be a real door -- some sets
# letter their openings A, B, C.
_MIN_LABEL_LEN = 3


def _join_split_mark(tag: str) -> str:
    """"105 B" is door 105B. Everything downstream matches on this string.

    Left alone it fails twice over: the plan prints 105B, so the door is never
    located, and the schedule reads as though 105 and B were separate marks. On
    one 34-door schedule every single row was split this way.
    """
    found = _SPLIT_MARK.match(tag.strip())
    return f"{found.group(1)}{found.group(2)}" if found else tag


def _looks_like_header(cells: list[str], header_strings: list[str]) -> bool:
    norm_row = {header_mapper.normalize(c) for c in cells if c}
    norm_hdr = {header_mapper.normalize(h) for h in header_strings if h}
    if not norm_row or not norm_hdr:
        return False
    return len(norm_row & norm_hdr) >= max(2, len(norm_row) // 2)


def _one_reading_per_table(found: list[PageExtraction]) -> list[PageExtraction]:
    """Drop a table we have already read, however we came to read it twice.

    Looking for every schedule on a sheet means offering the locator several
    header bands, and more than one of them can land on the same table -- a
    stray band on one sheet produced a third "table" that was the left-hand
    schedule again, its rows sheared across the wrong columns.

    Door numbers settle it. Two readings of one page that share most of their
    numbers are one table, and the better reading is the one that understood
    more columns, then the one with more rows. Nothing here can merge two
    genuinely different schedules: side by side or stacked, they have entirely
    different door numbers.
    """
    # More columns breaks the remaining ties. Two readings of one table differ
    # by where they took the header from, and a stacked header read at the group
    # row yields one column where the sheet has three: "DETAILS JAMB SILL HEAD"
    # holding all three details in a single cell. Neither reading maps those to
    # a canonical field, so they tied, and the merged one won on list order --
    # losing the split on every set built that way. Between two readings of the
    # same rows, the one that resolved more columns has strictly more of the
    # sheet in it.
    kept: list[PageExtraction] = []
    for extraction in sorted(
        found, key=lambda e: (-len({f for f in e.mapped if f}),
                              -len(e.headers), -len(e.rows))
    ):
        tags = {r.door_tag for r in extraction.rows if r.door_tag}
        if tags and any(
            e.page == extraction.page
            and len(tags & {r.door_tag for r in e.rows if r.door_tag})
            > len(tags) / 2
            for e in kept
        ):
            log.info("dropping a second reading of the same table on page %s "
                     "(%d rows)", extraction.page, len(extraction.rows))
            continue
        kept.append(extraction)
    return sorted(kept, key=lambda e: (e.page, -len(e.rows)))


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
            # Where the table sits, on both axes. Keyed on height alone, two
            # schedules printed side by side share a key and the second is
            # thrown away -- see page_finder.header_bands.
            key = (band.page, int(round(band.header_y)), int(round(band.tag_x)))
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

    out = _one_reading_per_table(out)

    # Only worth mentioning when it is the difference between rows and nothing:
    # otherwise it is noise on every multi-schedule sheet.
    if skipped and not any(e.rows for e in out):
        names = ", ".join(f"{e.title} ({len(e.rows)} rows)" for e in skipped)
        out.append(PageExtraction(
            skipped[0].page, ExtractionMethod.NONE, [], [],
            [f"Found no door schedule. Other schedules on this sheet: {names}."],
        ))
    return out




