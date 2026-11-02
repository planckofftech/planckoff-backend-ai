"""One reader per page layout, and which books use which.

A layout is named for how its prices are laid out, not for a brand: Allegion
prints several brands the same way, so one reader serves all of them once it is
shown to fit. A book is matched to its reader by file name, and only after the
reader has been checked against that book -- an unlisted book is reported as not
loaded rather than read by a reader that merely might fit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import fitz

from app.price_book import check
from app.price_book.readers import finish_columns, size_finish_grid
from app.price_book.rows import BookRead

Reader = Callable[[fitz.Document, str, str], BookRead]

# file name -> (manufacturer, reader). Checked against the book before listing.
BOOKS: dict[str, tuple[str, Reader]] = {
    "Von_Duprin_Price_Book_110058.pdf": ("Von Duprin", finish_columns.read),
    "2026_Price_Book_Complete.pdf": ("Hager", size_finish_grid.read),
}


def read_book(path: str | Path) -> BookRead:
    """Read a listed book and keep only the prices found on their own pages."""
    path = Path(path)
    if path.name not in BOOKS:
        raise KeyError(f"No reader is listed for {path.name}")
    manufacturer, reader = BOOKS[path.name]
    with fitz.open(path) as doc:
        book = reader(doc, manufacturer, path.name)
        check.verify(book, doc)
    for row in book.rows:
        row.book = path.name
    return book
