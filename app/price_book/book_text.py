"""A price book's whole text, page by page, for the pricing agent to search and read.

The chat that priced Schlage codes the way an estimator would did it by
searching the whole book and opening the pages its searches led to: the base
lock on one page, the cylinder on another, the indicator and BAA further on.
This gives the agent the same book: every page's text as printed, one printed
line per line with cells split by " | ", a search over all of it, and the
picture of any page.

Reading every page of the Schlage mechanical book takes about two seconds (460
pages, about a million characters, measured), so the text is read from the PDF
the first time a book is asked for and kept for the life of the process, not
stored. A handful of books are kept; the one used longest ago goes first.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import fitz

from app.price_book import page_text, pdfs

# Books held in memory at once.
_KEEP = 6
# What one search returns: the first pages in book order, a few lines from each,
# and only the numbers of the rest -- the agent narrows the search or opens one.
SEARCH_PAGES = 12
LINES_PER_PAGE = 4
_LINE_CHARS = 300
DPI = 110

_books: OrderedDict[str, "Book"] = OrderedDict()
_lock = threading.Lock()


@dataclass
class Hit:
    pdf_page: int
    lines: list[str]        # the lines the text is on, as printed (cut when long)
    matched: int            # how many lines of the page it is on


@dataclass
class Found:
    query: str
    hits: list[Hit]                                 # first pages, in book order
    more: list[int] = field(default_factory=list)   # further pages it is on


@dataclass
class Book:
    file_name: str
    path: Path
    pages: list[str]        # pdf page n is pages[n - 1]

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def page(self, pdf_page: int) -> str:
        """One page's text; "" for a page out of range or with no text layer."""
        return self.pages[pdf_page - 1] if 0 < pdf_page <= len(self.pages) else ""

    def search(self, query: str, first: int = 1, last: int | None = None,
               pages: int = SEARCH_PAGES) -> Found:
        """The pages from `first` to `last` a text is printed on, with its lines.

        A code like BAA or FSIC is printed in every series of a book; the range
        keeps a search inside the one section the book guide names.

        Letters are matched in any case, and a space or hyphen in the query
        matches any run of spaces and hyphens, or none: "CL 750-TLRC" finds
        "CL750 TLRC". The text must not run on into another code -- "L9050"
        does not find "LV9050" or "L9050R" -- unless the query ends in "*",
        which matches every code starting with it.
        """
        rx = pattern(query)
        if rx is None:
            return Found(query, [])
        hits: list[Hit] = []
        more: list[int] = []
        last = min(last or len(self.pages), len(self.pages))
        for n in range(max(first, 1), last + 1):
            text = self.pages[n - 1]
            lines = [line for line in text.split("\n") if rx.search(line)]
            if not lines:
                continue
            if len(hits) < pages:
                hits.append(Hit(n, [line[:_LINE_CHARS] for line in lines[:LINES_PER_PAGE]], len(lines)))
            else:
                more.append(n)
        return Found(query, hits, more)

    def image(self, pdf_page: int, dpi: int = DPI) -> bytes:
        """The page as a PNG."""
        with fitz.open(self.path) as doc:
            return doc[pdf_page - 1].get_pixmap(dpi=dpi).tobytes("png")


def pattern(query: str) -> re.Pattern[str] | None:
    query = page_text.clean(query)
    prefix = query.endswith("*")
    words = [w for w in re.split(r"[\s\-]+", query.rstrip("*")) if w]
    if not words:
        return None
    body = r"[\s\-]*".join(re.escape(w) for w in words)
    end = "" if prefix else r"(?![A-Za-z0-9])"
    return re.compile(r"(?<![A-Za-z0-9])" + body + end, re.IGNORECASE)


def read(path: Path, file_name: str) -> Book:
    with fitz.open(path) as doc:
        pages = ["\n".join(" | ".join(p.text for p in row)
                           for row in page_text.lines(page_text.pieces(page)))
                 for page in doc]
    return Book(file_name, path, pages)


def book(file_name: str) -> Book:
    """The book, read on first use; raises pdfs.BookMissing when there is none."""
    with _lock:
        if file_name in _books:
            _books.move_to_end(file_name)
            return _books[file_name]
    got = read(pdfs.path_of(file_name), file_name)
    with _lock:
        _books[file_name] = got
        _books.move_to_end(file_name)
        while len(_books) > _KEEP:
            _books.popitem(last=False)
    return got
