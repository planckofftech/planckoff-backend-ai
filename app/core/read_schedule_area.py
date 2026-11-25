"""Read the door schedule inside a box a person drew around it.

The schedule read finds its tables on its own, and sometimes finds the wrong
one, half of one, or none: a scan, a table drawn as linework, a schedule split
in two. A person who can see the table draws a box around it, and only what is
inside the box is read.

One box is one whole table -- headings, every section and the gaps between
them. Two tables are two boxes.

Same order as the schedule read, on the box alone:

  text    the box's own text and rulings, read by the same table reader the
          schedule read uses. No model, no cost.
  image   where the box holds too little text to read (a scan, a table drawn
          as outlines), or the text read is plainly partial, a picture of the
          box alone goes to the image reader. It wins only where it understood
          more of the table than the text did.

The person has already said this is a door schedule, so the bar for "is this
a table" is lower than when searching a whole sheet: a seven-door table in a
box is a table.

Nothing here logs why a table was or was not taken.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.core import extractor, page_finder
from app.core.extractor import PageExtraction, extract_page
from app.core.pdf_doc import PdfDoc, Rulings, Segment
from app.core.table_locator import TableNotFoundError
from app.schemas import DoorRow, ScheduleTable, TableBox

log = logging.getLogger(__name__)

# Fewer upright words than this inside the box and there is no text table to
# read: a scan, or headings and numbers drawn as linework.
_MIN_BOX_ITEMS = 8
# Door numbers a column needs under a heading. The sheet-wide search wants
# eight; a person drew this box around a door schedule, and a table of three
# doors is still a table.
_BOX_TAG_RUN = 1
# Where no band passes, the best one is still read if it has at least this
# much of a heading.
_LOOSE_HEADER_HITS = 2
# A box is widened by this fraction of the page before reading, so a line
# drawn on the table's own border is not cut off.
_EDGE = 0.004


def _inside(x0: float, y0: float, x1: float, y1: float,
            box: tuple[float, float, float, float]) -> bool:
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return box[0] <= cx <= box[2] and box[1] <= cy <= box[3]


class _Within:
    """A document that shows one page's box and nothing else of that page.

    The table reader is handed this instead of the document, so everything it
    already knows -- ruled and banded grids, stacked headings, bands, metric
    sizes -- works on the box exactly as it works on a sheet, and text outside
    the box can never land in a column.
    """

    def __init__(self, doc: PdfDoc, page: int, box: tuple[float, float, float, float]):
        self._doc = doc
        self._index = page - 1
        self._box = box  # page points, as displayed

    def __getattr__(self, name):
        return getattr(self._doc, name)

    def text_items(self, page_index: int):
        items = self._doc.text_items(page_index)
        if page_index != self._index:
            return items
        return [i for i in items if _inside(i.x0, i.y0, i.x1, i.y1, self._box)]

    def rulings(self, page_index: int) -> Rulings:
        rulings = self._doc.rulings(page_index)
        if page_index != self._index:
            return rulings
        x0, y0, x1, y1 = self._box

        def clip(segments: list[Segment], lo: float, hi: float, a: float, b: float
                 ) -> list[Segment]:
            # pos within [lo, hi]; the run clipped to [a, b].
            out = []
            for s in segments:
                if lo <= s.pos <= hi and s.end > a and s.start < b:
                    out.append(Segment(s.pos, max(s.start, a), min(s.end, b)))
            return out

        return Rulings(clip(rulings.vertical, x0, x1, y0, y1),
                       clip(rulings.horizontal, y0, y1, x0, x1))


def _bands(items, page: int, min_header_hits: int) -> list[page_finder.PageCandidate]:
    """The header bands in the box to read a table from.

    The sheet's own rules first -- `page_finder.header_bands`, every one of
    them, short only of the sheet's minimum count of words on the page and of
    door numbers in a column. Those rules are what refuse a second reading of
    the same table from a band with no real door column under it; lowering
    the bar for them read one 50-door table twice, the second time with room
    numbers for door numbers.

    Only where nothing passes them is the bar lowered, and then for the one
    best band: the person has said a door schedule is here.
    """
    horizontal = page_finder.upright(items)
    bands = page_finder._scored_bands(horizontal)
    found: list[page_finder.PageCandidate] = []
    loose: list[page_finder.PageCandidate] = []
    for index, (words, header_y, band, leaf) in enumerate(bands):
        if len(words) < min(min_header_hits, page_finder._POOL_MIN_WORDS):
            continue
        candidate = page_finder._judge(horizontal, band, words, header_y, page,
                                       min_header_hits, _BOX_TAG_RUN, leaf,
                                       page_finder._pooled(bands, index))
        borrowed = (len(words) < min_header_hits
                    or not words & page_finder._DOOR_MARKERS)
        if borrowed and any(abs(candidate.tag_x - c.tag_x) <= page_finder._SAME_TABLE_X
                            for c in found):
            continue
        if any(abs(header_y - c.header_y) <= page_finder._SAME_TABLE_Y
               and abs(candidate.tag_x - c.tag_x) <= page_finder._SAME_TABLE_X
               for c in found):
            continue
        if candidate.passed:
            found.append(candidate)
        elif (candidate.header_hits >= _LOOSE_HEADER_HITS
              and candidate.tag_run >= _BOX_TAG_RUN):
            loose.append(candidate)
    if not found and loose:
        found = [max(loose, key=lambda c: c.score)]
    return sorted(found, key=lambda c: c.header_y)


def read_text(doc: PdfDoc, page: int, box: tuple[float, float, float, float]
              ) -> tuple[list[PageExtraction], bool]:
    """The tables in the box read from its text, and whether the box had any
    text worth reading at all (False: a scan, or text drawn as outlines)."""
    within = _Within(doc, page, box)
    items = within.text_items(page - 1)
    if len(page_finder.upright(items)) < _MIN_BOX_ITEMS:
        return [], False
    from app.config import get_settings

    read: list[PageExtraction] = []
    for band in _bands(items, page, get_settings().min_header_hits):
        try:
            extraction = extract_page(within, band)
        except (TableNotFoundError, IndexError, ValueError):
            continue
        if extraction.rows:
            read.append(extraction)
    return _judged(extractor._one_reading_per_table(read)), True


def _judged(found: list[PageExtraction]) -> list[PageExtraction]:
    """The tables the schedule read's own judge would keep.

    A box drawn around a schedule often takes in its legend too -- the
    materials key beside it reads as a table of "doors" named AL and HM. The
    schedule read never keeps such a table, because every table it reads
    passes this judge first; a box gets the same.

    Judged on the tables alone, not as a sheet the index names: told that,
    the judge passed the legend too. The tables it finds stand; only where it
    finds none do the doubtful ones; only where it rejects every one does the
    largest -- the person drew around a table, and an answer to check beats
    none.
    """
    from app.core import schedule_judge

    return _best_verdicts(found, lambda e: schedule_judge.judge(
        e, index_says_door=None).outcome, lambda e: len(e.rows))


@dataclass(slots=True)
class BoxRead:
    """What one drawn box gave."""

    page: int
    tables: list[ScheduleTable] = field(default_factory=list)
    method: str = ""        # "text" | "image" | ""
    warnings: list[str] = field(default_factory=list)


def _as_tables(found: list[PageExtraction]) -> list[ScheduleTable]:
    from app.pipeline import _table_box

    return [ScheduleTable(title=e.title, page=e.page, headers=e.headers,
                          field_map=e.mapped, row_count=len(e.rows), rows=e.rows,
                          box=_table_box(e))
            for e in found]


async def read_image(doc: PdfDoc, page: int, box: tuple[float, float, float, float],
                     warnings: list[str]) -> list[ScheduleTable]:
    """The box as a picture, read by the image reader. Each table's box comes
    back as fractions of the page. Raises AiUpstreamError where the provider
    refused, so the caller can tell that apart from an empty table."""
    from app.ai import vision_extract as vx
    from app.ai.client import AiUnavailableError, get_client
    from app.config import get_settings

    settings = get_settings()
    try:
        client = get_client()
    except AiUnavailableError:
        return []
    width, height = doc.page_size(page - 1)
    png = vx._render_within_limit(doc, page, vx._CROP_DPI, clip=box)
    parsed = await vx._ask(client, settings, png)

    # Read off the page where the page prints its door numbers as text, the
    # model's numbers are checked against them, as the schedule read does.
    printed = {i.text.strip() for i in _Within(doc, page, box).text_items(page - 1)}
    fx0, fy0 = box[0] / width, box[1] / height
    fw, fh = (box[2] - box[0]) / width, (box[3] - box[1]) / height
    out: list[ScheduleTable] = []
    candidates = []
    for title, headers, raw_rows, model_box, parse_warnings in parsed:
        rows, map_warnings = await vx.rows_from_table(headers, raw_rows)
        if not rows:
            continue
        tags = [r.door_tag.strip() for r in rows if r.door_tag and r.door_tag.strip()]
        share = sum(t in printed for t in tags) / len(tags) if tags else None
        candidates.append((title, headers, rows, model_box,
                           parse_warnings + map_warnings, share))
    checkable = any(s is not None and s >= vx._PRINTED_SHARE for *_, s in candidates)
    for title, headers, rows, model_box, more, share in candidates:
        if checkable and share is not None and share < vx._PRINTED_SHARE:
            continue
        if model_box:
            bx0, by0, bx1, by1 = model_box
            placed = TableBox(page=page, source="model",
                              x0=fx0 + bx0 * fw, y0=fy0 + by0 * fh,
                              x1=fx0 + bx1 * fw, y1=fy0 + by1 * fh)
        else:
            placed = TableBox(page=page, source="model", x0=fx0, y0=fy0,
                              x1=fx0 + fw, y1=fy0 + fh)
        warnings.extend(more)
        title = "" if extractor.is_other_schedule(title) else title
        out.append(ScheduleTable(title=title, page=page, headers=headers,
                                 row_count=len(rows), rows=rows, box=placed))
    return _judged_images(out)


def _judged_images(tables: list[ScheduleTable]) -> list[ScheduleTable]:
    """The image reader's tables, judged as the schedule read judges them --
    see `_judged`."""
    from app.core import schedule_judge
    from app.pipeline import _ReadTable

    def verdict(t: ScheduleTable) -> str:
        fields = sorted({f for r in t.rows for f, v in r.model_dump().items()
                         if v and f not in ("extra", "level")})
        return schedule_judge.judge(_ReadTable(t.page, t.title, t.headers, t.rows, fields),
                                    index_says_door=None).outcome

    return _best_verdicts(tables, verdict, lambda t: len(t.rows))


def _best_verdicts(tables: list, verdict, size) -> list:
    """Found tables; only where none is found, the unsure ones; only where
    every one is rejected, the largest -- the person drew around a table."""
    from app.core import schedule_judge

    if not tables:
        return []
    outcome = [(t, verdict(t)) for t in tables]
    for wanted in (schedule_judge.FOUND, schedule_judge.UNSURE):
        kept = [t for t, o in outcome if o == wanted]
        if kept:
            return kept
    return [max(tables, key=size)]


async def read_box(doc: PdfDoc, page: int, box_fraction: tuple[float, float, float, float],
                   *, ai: bool) -> BoxRead:
    """Text first; the image reader where the text gave nothing or too little.

    `box_fraction` is the box as fractions of the page as displayed. `ai`
    False reads text only.
    """
    from app.ai.client import AiUpstreamError
    from app.pipeline import _is_thin

    width, height = doc.page_size(page - 1)
    x0, y0, x1, y1 = box_fraction
    box = (max(0.0, x0 - _EDGE) * width, max(0.0, y0 - _EDGE) * height,
           min(1.0, x1 + _EDGE) * width, min(1.0, y1 + _EDGE) * height)
    result = BoxRead(page)

    found, has_text = read_text(doc, page, box)
    widest = max(found, key=lambda e: len(e.rows), default=None)
    thin = widest is None or _is_thin(widest) is not None
    if found:
        result.tables, result.method = _as_tables(found), "text"
        for e in found:
            result.warnings.extend(e.warnings)
    if not thin or not ai:
        return result

    image_warnings: list[str] = []
    try:
        pictured = await read_image(doc, page, box, image_warnings)
    except AiUpstreamError as exc:
        log.error("schedule area: image reader refused on p%s: %s", page, exc)
        if not found:
            raise
        return result
    if not pictured:
        return result
    if widest is not None and max(len(t.headers) for t in pictured) <= len(widest.headers):
        return result  # the text read understood as much; it stands
    result.tables, result.method = pictured, "image"
    result.warnings = image_warnings
    return result


def merge_boxes(drawn: list[tuple[int, tuple[float, float, float, float]]]
                ) -> list[tuple[int, tuple[float, float, float, float]]]:
    """Boxes on one page that overlap are one box, read once."""
    out: list[tuple[int, list[float]]] = []
    for page, b in sorted(drawn):
        box = list(b)
        merged = True
        while merged:
            merged = False
            for i, (p, o) in enumerate(out):
                if p == page and box[0] < o[2] and o[0] < box[2] and box[1] < o[3] and o[1] < box[3]:
                    box = [min(box[0], o[0]), min(box[1], o[1]),
                           max(box[2], o[2]), max(box[3], o[3])]
                    out.pop(i)
                    merged = True
                    break
        out.append((page, box))
    return [(p, tuple(b)) for p, b in sorted(out)]


def finish(tables: list[ScheduleTable], warnings: list[str]) -> list[ScheduleTable]:
    """The same last steps the schedule read takes: a sidelite table naming
    other tables' doors left out, a row listing several doors split, and a
    level taken from captions that name one."""
    from app.pipeline import _drop_attached, _levels_from_titles, _one_row_per_door

    tables = _drop_attached(tables, warnings)
    _one_row_per_door(tables, warnings)
    _levels_from_titles(tables)
    return tables


def door_rows(tables: list[ScheduleTable]) -> list[DoorRow]:
    return [r for t in tables for r in t.rows]
