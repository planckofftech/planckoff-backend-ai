"""The drawing index printed on the cover sheet, and where each sheet it lists is.

Almost every set opens with one -- SHEET INDEX, DRAWING INDEX, INDEX OF
DRAWINGS, DRAWING LIST, SHEET LIST -- on the first two or three pages. It is the
architect's own list of what the set holds, and it is right far more often
than anything read off the sheets themselves.

What the index looks like varies; what it is does not. Every one of them is a
column of sheet numbers with each sheet's title set immediately to its right:

    A6-001   SCHEDULE DOOR, LEGEND DOOR & FRAME   ●
    A-5.02   COMMON AREA DOOR SCHEDULE            ⚫
    A05-01   DOOR & WINDOW SCHEDULE

So the index is found by that shape, not by its heading -- one set prints its
list with no heading at all -- and a list split into several column groups is
several such columns.

A listed sheet is placed on a page only when that page's own title block
carries the same sheet number. The order of the list says where to look first;
the title block decides. A sheet the index lists that no page carries is
reported as missing rather than guessed at: one residential set lists a unit
door schedule and a common area door schedule, and the file holds neither.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from app.core.pdf_doc import PdfDoc

# A sheet number: A6-001, A-5.02, A05-01, AS600, CR1.04, A7.01-9.
_NUMBER = re.compile(
    r"^[A-Z]{1,3}[-.]?\d{1,4}(?:[.\-]\d{1,3}){0,2}[A-Za-z]?$")
# How many of the first pages may hold the index. Two volumes, a cover and a
# code sheet are the most seen in front of it.
_SEARCH_PAGES = 6
# Fewest numbers standing in one column for it to be a sheet list rather than
# a few detail references in a note.
_MIN_COLUMN = 8
# Two sheet numbers are in the same column when their left edges are this
# close, in points.
_COLUMN_X = 6.0
# A scanned cover is read in two passes: the whole sheet just sharp enough to
# find the index heading, then the block under it sharp enough for its small
# print. Only the first few sheets -- the index is never further in.
_OCR_SEARCH_PAGES = 3
# Fewer text items than this and a page has no real text layer -- a scan with
# a stamp or a few words left on it. The page finder draws the line here too.
_TEXT_LAYER = 40
_OCR_PAGE_PX = 1650
_OCR_BLOCK_DPI = 300
_MIN_OCR_DPI, _MAX_OCR_DPI = 72, 200
_HEADING = re.compile(
    r"(SHEET|DRAWING)S?\s*(INDEX|LIST)|INDEX\s+OF\s+(DRAWINGS|SHEETS)"
    r"|LIST\s+OF\s+(DRAWINGS|SHEETS)", re.I)
# How much of the sheet under the heading to read: an index runs down a column
# and rarely wider than a quarter of the sheet.
_CLIP_PAD = 20.0
_CLIP_WIDTH = 0.30
_CLIP_HEIGHT = 0.60
# The title starts right after the number: no further away than this many
# times the number's own height. Further is another column of the sheet.
_TITLE_REACH = 12.0
# A title is words. A date, an issue mark, a revision number is not.
_TITLE = re.compile(r"[A-Za-z]{3}")
# A page's own sheet number is set near the largest size on the page; a
# reference to another sheet is a fraction of it.
_OWN_NUMBER = 0.5
# How far from where the list says a sheet should be to look before giving up
# on order and searching the whole file.
_NEAR = 12
# Where a title block may be read as an image: this many pages either side of
# where the list says the sheet is, and this many reads per document in all.
_OCR_NEAR = 2
_OCR_BUDGET = 12


@dataclass(slots=True)
class IndexEntry:
    number: str
    title: str
    page: int | None = None  # 1-indexed, once a title block confirms it
    # Placed by counting from a sheet whose number could be read, because its
    # own title block could not be.
    counted: bool = False


def _normal(number: str) -> str:
    """A sheet number as it compares: A-5.01, A5.01 and A5-01 are one sheet."""
    return re.sub(r"[-.\s]", "", number.upper())


def read_entries(doc: PdfDoc) -> list[IndexEntry]:
    """Every sheet the cover index lists, in its order. [] if there is none."""
    for page_index in range(min(_SEARCH_PAGES, doc.page_count)):
        entries = _entries_from(doc.text_items(page_index))
        if entries:
            return entries
    # A scanned set: the front sheets carry no text at all. Recognise them,
    # but only the few that can hold the index, and only where it is.
    for page_index in range(min(_OCR_SEARCH_PAGES, doc.page_count)):
        if len(doc.text_items(page_index)) >= _TEXT_LAYER:
            continue
        entries = _entries_by_ocr(doc, page_index + 1)
        if entries:
            return entries
    return []


def _entries_by_ocr(doc: PdfDoc, page: int) -> list[IndexEntry]:
    """The index on a scanned sheet: find its heading, then read only under it.

    A whole sheet recognised at a resolution that keeps it affordable reads its
    headings and loses its small print -- one cover gave five legible sheet
    numbers out of fifty. The heading survives, so it says where to look; that
    block alone, read at full resolution, gave all fifty-two.
    """
    from app.core import ocr

    if not ocr.available():
        return []
    try:
        width, height = doc.page_size(page - 1)
        page_items = ocr.read(doc, page, dpi=_dpi_for(width, _OCR_PAGE_PX))
    except Exception:  # noqa: BLE001 - an unreadable cover is not fatal
        return []
    for heading in (i for i in page_items if _HEADING.search(i.text)):
        clip = (max(0.0, heading.x0 - _CLIP_PAD), max(0.0, heading.y0 - _CLIP_PAD),
                min(width, heading.x0 + width * _CLIP_WIDTH),
                min(height, heading.y0 + height * _CLIP_HEIGHT))
        try:
            block = ocr.read(doc, page, clip=clip, dpi=_OCR_BLOCK_DPI)
        except Exception:  # noqa: BLE001
            continue
        entries = _entries_from(block)
        if entries:
            return entries
    return []


def _dpi_for(width_pt: float, pixels: int) -> int:
    """A resolution that renders this page `pixels` wide, within sane bounds."""
    return max(_MIN_OCR_DPI, min(_MAX_OCR_DPI, round(pixels * 72 / max(width_pt, 1.0))))


def _entries_from(items) -> list[IndexEntry]:
    items = [i for i in items if i.horizontal]
    numbers = [i for i in items if _NUMBER.match(i.text.strip())]
    if len(numbers) < _MIN_COLUMN:
        return []

    columns: dict[int, list] = defaultdict(list)
    for item in sorted(numbers, key=lambda i: i.x0):
        key = next((k for k in columns if abs(k - item.x0) <= _COLUMN_X),
                   round(item.x0))
        columns[key].append(item)

    entries: list[IndexEntry] = []
    for key in sorted(columns):
        column = columns[key]
        if len(column) < _MIN_COLUMN:
            continue
        found = [e for e in (_entry(n, items) for n in
                             sorted(column, key=lambda i: i.y0)) if e]
        # A column of numbers with no titles beside them is a revision
        # table or a grid, not a sheet list.
        if len(found) >= _MIN_COLUMN:
            entries += found
    return entries


def _entry(number, items) -> IndexEntry | None:
    height = max(number.y1 - number.y0, 1.0)
    beside = [i for i in items
              if i is not number
              and abs(i.cy - number.cy) <= height * 0.6
              and 0 <= i.x0 - number.x1 <= height * _TITLE_REACH]
    for item in sorted(beside, key=lambda i: i.x0):
        text = item.text.strip()
        if _NUMBER.match(text):
            return None
        if _TITLE.search(text):
            return IndexEntry(number.text.strip(), re.sub(r"\s+", " ", text))
    return None


def normal_number(number: str) -> str:
    """A sheet number as the index and title blocks are compared."""
    return _normal(number)


def own_number(doc: PdfDoc, page: int, listed: dict[str, IndexEntry]
               ) -> IndexEntry | None:
    """The index entry this page prints as its own number, or None.

    The largest text on the page that the index lists. References to other
    sheets -- 3/A-404 under a section mark -- are set smaller than the title
    block's own number, and grid bubbles are not in the index at all.
    """
    best = None
    for item in doc.text_items(page - 1):
        entry = listed.get(_normal(item.text.strip()))
        if entry is not None and (best is None or item.size > best[0]):
            best = (item.size, entry)
    return best[1] if best else None


def sheet_number_of(doc: PdfDoc, page: int, *, ocr: bool = True) -> str:
    """The sheet number in this page's title block, normalised, or ''.

    The largest text on the page shaped like a sheet number. Not "a sheet
    number among the largest few texts": one sheet sets twelve detail titles a
    point larger than its own number, and the number fell off the end of that
    list. A cross-reference to another sheet is small; the sheet's own number
    is set to be read across the room.
    """
    items = doc.text_items(page - 1)
    if items:
        largest = max(i.size for i in items)
        numbers = [i for i in items if _NUMBER.match(i.text.strip())
                   and i.size >= largest * _OWN_NUMBER]
        if numbers:
            return _normal(max(numbers, key=lambda i: i.size).text.strip())
    if len(items) >= _TEXT_LAYER or not ocr:
        return ""
    # A scanned sheet: read its title block as an image. Bounded by the same
    # per-document budget every other title-block read shares.
    from app.core.plan_index import _ocr_title_block

    return next((_normal(t) for t in _ocr_title_block(doc, page)
                 if _NUMBER.match(t.strip())), "")


def locate(doc: PdfDoc, entries: list[IndexEntry],
           wanted: list[IndexEntry] | None = None) -> list[IndexEntry]:
    """Put `wanted` entries (default: all) on their pages, by title block.

    The list's own order is the first guess -- the k-th sheet listed is near
    page k, give or take the cover sheets -- so a sheet is usually confirmed in
    one or two reads, and only a sheet out of order costs a search.

    Reading a title block as an image costs seconds a page, so it is spent
    only where the list says the sheet should be -- a few pages either side --
    and within a budget for the whole document. Further out the search uses
    the text layer alone. Searching every page by image once took a set of
    outlined sheets past ten minutes for one door schedule sheet; a sheet that
    cannot be confirmed cheaply is simply not used as a lead.
    """
    order = {_normal(e.number): k for k, e in enumerate(entries)}
    offset = 0
    read: dict[int, str] = {}
    budget = [_OCR_BUDGET]

    def number_on(page: int, near_guess: bool) -> str:
        if page in read:
            return read[page]
        text_only = sheet_number_of(doc, page, ocr=False)
        if text_only or not near_guess or budget[0] <= 0:
            if text_only or not near_guess:
                read[page] = text_only
            return text_only
        budget[0] -= 1
        read[page] = sheet_number_of(doc, page)
        return read[page]

    for entry in (wanted if wanted is not None else entries):
        target = _normal(entry.number)
        guess = order.get(target, 0) + 1 + offset
        near = [guess + d for step in range(_NEAR + 1) for d in ((step, -step) if step else (0,))]
        pages = [p for p in near if 1 <= p <= doc.page_count]
        pages += [p for p in range(1, doc.page_count + 1) if p not in pages]
        for page in pages:
            if number_on(page, abs(page - guess) <= _OCR_NEAR) == target:
                entry.page = page
                # Sheets after this one are shifted by the same amount.
                offset = page - (order.get(target, 0) + 1)
                break

    # A sheet drawn as a picture has no number to read, and without OCR none
    # can be read from it. One set exports its whole architectural part as
    # pictures -- 136 of 174 pages -- and its door schedule sheet A-607 was
    # never placed, so the set read as having no door schedule. The index is
    # still in page order. The nearest sheet whose number could be read pins
    # that order to the file -- C101, the index's 71st entry, on page 73 -- and
    # counting from it puts A-607, the 65th, on page 67. What is read there is
    # judged like any other table, so a wrong count reads as no schedule.
    anchors = {order[n]: page for page, n in
               ((page, _normal(number)) for page, number in read.items() if number)
               if n in order}
    for entry in (wanted if wanted is not None else entries):
        if entry.page is not None or not anchors:
            continue
        k = order.get(_normal(entry.number))
        if k is None:
            continue
        nearest = min(anchors, key=lambda a: abs(a - k))
        if abs(nearest - k) > _NEAR:
            continue
        page = anchors[nearest] + (k - nearest)
        if not 1 <= page <= doc.page_count or read.get(page):
            continue  # off the file, or a page that reads as another sheet
        entry.page, entry.counted = page, True
    return wanted if wanted is not None else entries
