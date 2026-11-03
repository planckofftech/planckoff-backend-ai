"""A book's own table of contents: one short line per page, made by code.

Picking the page a product is priced on cannot be done by searching for the
model: Hager prints "5200" on 27 of its 744 pages, most of them not the 5200
price page. A person flips to the section instead, and this is what lets the AI
do the same without being sent the book -- the page headings, how many prices
each page prints, and the models priced on it:

    p147 [16] Door Controls - 5200 Series | 48 prices | 5200, 5200DA, 5200PA ...

Two things are read off every page:

  heading      the text printed larger than the page's body, near the top,
               after leaving out what repeats on most pages of the book (a
               tab bar of chapters, "Effective July 13, 2026", a finish chart)
  models       codes on the lines that carry a price, then the rest

and, where a book prints one, its index of part numbers with the page each is
on (Corbin Russwin's "Part Number | Page"). A page with no text at all is kept
with `has_text` false: it is a picture, and only a read of the image says what
is on it.

Nothing here prices anything; it only says where to look.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

import fitz

from app.price_book import page_text
from app.price_book.page_text import Piece
from app.price_book.readers.common import printed_page
from app.price_book.readers.table_rows import codes_in, is_price

# A line on more than this share of a book's pages is the book's furniture,
# not the page's subject.
_REPEATS = 0.4
# ...and one in the top or bottom band needs to repeat on only this share:
# running headers are the furniture most often printed there.
_REPEATS_EDGE = 0.2
_EDGE = 0.12
# The heading is looked for in the top part of the page.
_TOP = 0.35
# A heading is printed at least this much larger than the page's body text.
_LARGER = 1.15
_HEADING_CHARS = 150
_MODELS = 25
# An index line pairs a part number with a page number; a page is an index
# when it holds at least this many such pairs.
_INDEX_PAIRS = 15
_PAGE_NO = re.compile(r"^\d{1,4}$")
_LEADER = re.compile(r"\.{3,}")
# Words codes_in takes that are not models: phone numbers and page ranges
# (800-325-9995, 23-29), years, short bare numbers (hinge widths, competitor
# columns), and measures written in words (3-foot, 44mm, 6-pin).
_NOT_MODEL = re.compile(r"^(\d+(-\d+)+|(19|20)\d\d|\d{1,3})$|[a-z]{2,}$")
# An index says so in a column heading ("Part Number | Page") or with dot
# leaders; a list of item numbers (Hager's) has neither.
_PAGE_HEAD = re.compile(r"^page$", re.I)


@dataclass
class PageEntry:
    pdf_page: int
    printed_page: str
    heading: str
    prices: int
    models: list[str] = field(default_factory=list)
    has_text: bool = True

    def line(self) -> str:
        """The entry as one line of the map an AI is shown."""
        printed = f" [{self.printed_page}]" if self.printed_page else ""
        if not self.has_text:
            return f"p{self.pdf_page}{printed} (picture page, no text)"
        parts = [f"p{self.pdf_page}{printed} {self.heading or '-'}", f"{self.prices} prices"]
        if self.models:
            parts.append(", ".join(self.models))
        return " | ".join(parts)


@dataclass
class IndexEntry:
    model: str
    printed_page: str
    index_page: int
    pdf_page: int | None = None


@dataclass
class BookMap:
    file_name: str
    pages: list[PageEntry]
    index: list[IndexEntry]

    def text(self) -> str:
        return "\n".join(p.line() for p in self.pages)


def map_book(doc: fitz.Document, file_name: str) -> BookMap:
    read = [_page_lines(doc[i]) for i in range(doc.page_count)]
    furniture = _furniture(read)
    pages, index = [], []
    for i, (rows, height) in enumerate(read):
        number = i + 1
        printed = printed_page(doc[i].get_text()) or _edge_number(rows, height)
        if not rows:
            pages.append(PageEntry(number, printed, "", 0, has_text=False))
            continue
        kept = [r for r in rows if _norm(r) not in furniture]
        pages.append(PageEntry(number, printed, _heading(kept, height),
                               sum(page_text.count_prices(r) for r in rows), _models(kept)))
        index.extend(_index(rows, number, doc.page_count))
    _place(index, pages)
    return BookMap(file_name, pages, index)


def _page_lines(page: fitz.Page) -> tuple[list[list[Piece]], float]:
    return page_text.lines(page_text.pieces(page)), page.rect.height


def _norm(row: list[Piece]) -> str:
    # Page numbers and dates change from page to page; the furniture does not.
    return re.sub(r"\d+", "#", " ".join(p.text for p in row)).upper()


def _furniture(read: list[tuple[list[list[Piece]], float]]) -> set[str]:
    anywhere: dict[str, int] = {}
    edge: dict[str, int] = {}
    for rows, height in read:
        seen, seen_edge = set(), set()
        for r in rows:
            key = _norm(r)
            seen.add(key)
            y = r[0].yc / height if height else 0.5
            if y < _EDGE or y > 1 - _EDGE:
                seen_edge.add(key)
        for k in seen:
            anywhere[k] = anywhere.get(k, 0) + 1
        for k in seen_edge:
            edge[k] = edge.get(k, 0) + 1
    with_text = sum(1 for rows, _ in read if rows)
    if with_text < 4:
        return set()
    return ({k for k, n in anywhere.items() if n > _REPEATS * with_text}
            | {k for k, n in edge.items() if n > _REPEATS_EDGE * with_text})


def _heading(rows: list[list[Piece]], height: float) -> str:
    if not rows:
        return ""
    body = statistics.median(p.size for r in rows for p in r)
    top = [r for r in rows if r[0].yc <= height * _TOP]
    big = [p.text for r in top for p in r
           if p.size >= body * _LARGER and not is_price(p) and not _PAGE_NO.match(p.text)]
    if not big:
        # No larger type (Don-Jo prints straight lists): the first line says
        # what the page is, if anything does.
        first = next((r for r in rows if not all(_PAGE_NO.match(p.text) for p in r)), [])
        big = [p.text for p in first if not is_price(p)]
    out: list[str] = []
    for t in big:
        if t not in out:
            out.append(t)
    return " - ".join(out)[:_HEADING_CHARS].strip(" -")


def _models(rows: list[list[Piece]]) -> list[str]:
    priced, other = [], []
    for r in rows:
        into = priced if any(is_price(p) for p in r) else other
        for p in r:
            for word in _LEADER.sub(" ", p.text).split():
                into.extend(c for c in codes_in(word.strip("*†,;:().")) if not _NOT_MODEL.search(c))
    out: list[str] = []
    for c in priced + other:
        if c not in out:
            out.append(c)
        if len(out) == _MODELS:
            break
    return out


def _edge_number(rows: list[list[Piece]], height: float) -> str:
    """A page number printed alone at the top or bottom, for books the header
    patterns in common.py do not know."""
    for r in rows[:2] + rows[-2:]:
        y = r[0].yc / height if height else 0.5
        if len(r) == 1 and _PAGE_NO.match(r[0].text) and (y < _EDGE or y > 1 - _EDGE):
            return r[0].text
    return ""


def _index(rows: list[list[Piece]], pdf_page: int, page_count: int) -> list[IndexEntry]:
    """Part number -> page pairs, when the page is an index of them.

    A pair is a code followed by a page number on the same line: side by side
    as two pieces ("418F352 | 423"), or after dot leaders in one ("F75 ..... 12").
    A page with only a few such pairs, or with no "Page" column and no
    leaders, is a table that happens to hold numbers.
    """
    if not any(_PAGE_HEAD.match(p.text) or _LEADER.search(p.text) for r in rows for p in r):
        return []
    pairs: list[IndexEntry] = []
    for r in rows:
        texts = [t for p in r for t in _LEADER.sub(" ", p.text).split()]
        for a, b in zip(texts, texts[1:]):
            code = codes_in(a.strip("*†"))
            if code and _PAGE_NO.match(b) and 0 < int(b) <= page_count * 2:
                pairs.append(IndexEntry(code[0], b, pdf_page))
    return pairs if len(pairs) >= _INDEX_PAIRS else []


def _place(index: list[IndexEntry], pages: list[PageEntry]) -> None:
    """The PDF page each indexed printed page is, where the book says it once."""
    where: dict[str, list[int]] = {}
    for p in pages:
        if p.printed_page:
            where.setdefault(p.printed_page, []).append(p.pdf_page)
    for e in index:
        found = where.get(e.printed_page, [])
        if len(found) == 1:
            e.pdf_page = found[0]
