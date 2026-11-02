"""Every kept price must be printed on the page its row names.

Readers take prices off the page, so this mostly guards the readers themselves:
a row whose page or price was carried over wrongly fails here instead of being
stored. A row that fails is moved to `rejected`, never corrected.
"""

from __future__ import annotations

import fitz

from app.price_book.page_text import pieces
from app.price_book.rows import BookRead


def verify(book: BookRead, doc: fitz.Document) -> int:
    """Drop rows whose price is not on their page. Returns how many were dropped."""
    printed: dict[int, set[str]] = {}
    kept = []
    for row in book.rows:
        if row.pdf_page not in printed:
            words: set[str] = set()
            for p in pieces(doc[row.pdf_page - 1]):
                words.add(p.text)
                words.update(p.text.split())
            printed[row.pdf_page] = words
        if row.price_text in printed[row.pdf_page]:
            kept.append(row)
        else:
            book.rejected.append(
                f"p.{row.pdf_page}: {row.model} {row.finish} {row.price_text} "
                "is not printed on that page")
    dropped = len(book.rows) - len(kept)
    book.rows = kept
    return dropped
