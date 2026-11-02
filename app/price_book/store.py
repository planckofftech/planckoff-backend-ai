"""Price books in Supabase: written by the loader script, read by the price lookup.

Reading a book again replaces it. The old book row is deleted first and its rows
go with it (on delete cascade), so a model never has two answers from two reads
of one file. The delete and the inserts are not one transaction: a loader that
dies half way leaves the book partly loaded, and the fix is to run it again.
"""

from __future__ import annotations

import difflib
import logging
from typing import Any

from app.db.client import client
from app.price_book.lookup import Book
from app.price_book.rows import BookRead, PriceRow

log = logging.getLogger(__name__)

_BATCH = 500
# PostgREST returns at most this many rows per request, whatever was asked.
_PAGE = 1000


def save(book: BookRead, sha256: str) -> str:
    """Store a book that was read, replacing any earlier read of the same file."""
    db = client()
    db.table("price_books").delete().eq("file_name", book.file_name).execute()
    made = db.table("price_books").insert({
        "manufacturer": book.manufacturer,
        "file_name": book.file_name,
        "sha256": sha256,
        "reader": book.reader,
        "effective_date": book.effective_date,
        "page_count": book.page_count,
        "rows_read": len(book.rows),
        "pages_unread": len(book.unread),
        "prices_unread": sum(u.prices_on_page for u in book.unread),
    }).execute()
    book_id = made.data[0]["id"]

    rows = [_row(book_id, r) for r in book.rows]
    for i in range(0, len(rows), _BATCH):
        db.table("price_rows").insert(rows[i:i + _BATCH]).execute()
    gaps = [{"book_id": book_id, "pdf_page": u.pdf_page,
             "prices_on_page": u.prices_on_page, "reason": u.reason} for u in book.unread]
    for i in range(0, len(gaps), _BATCH):
        db.table("price_book_gaps").insert(gaps[i:i + _BATCH]).execute()
    log.info("price book %s: %d rows stored", book.file_name, len(rows))
    return book_id


def _row(book_id: str, r: PriceRow) -> dict[str, Any]:
    return {
        "book_id": book_id, "manufacturer": r.manufacturer, "model": r.model,
        "match_key": r.key, "kind": r.kind, "finish": r.finish, "size": r.size,
        "list_price": r.list_price, "price_text": r.price_text, "pdf_page": r.pdf_page,
        "printed_page": r.printed_page, "description": r.description, "note": r.note,
    }


def loaded_books() -> list[dict[str, Any]]:
    """Every book stored, with how much of it was read."""
    return (client().table("price_books")
            .select("id, manufacturer, file_name, reader, effective_date, page_count, "
                    "rows_read, pages_unread, prices_unread, loaded_at")
            .order("manufacturer").execute().data)


class DbSource:
    """Rows for the lookup, read from Supabase.

    One per request: the list of books is read once and kept for the request,
    so pricing a hardware set of forty items asks for it once, not forty times.
    """

    def __init__(self) -> None:
        self._db = client()
        self._books: dict[str, Book] | None = None
        self._file_of: dict[str, str] = {}

    def books(self) -> dict[str, Book]:
        if self._books is None:
            found = (self._db.table("price_books")
                     .select("id, manufacturer, file_name, effective_date").execute().data)
            self._books = {b["file_name"]: Book(b["manufacturer"], b["file_name"],
                                                b["effective_date"]) for b in found}
            self._file_of = {b["id"]: b["file_name"] for b in found}
        return self._books

    def rows(self, manufacturer: str, keys: set[str]) -> list[PriceRow]:
        self.books()
        out: list[PriceRow] = []
        start = 0
        while True:
            got = (self._db.table("price_rows").select("*")
                   .eq("manufacturer", manufacturer).in_("match_key", sorted(keys))
                   .order("id").range(start, start + _PAGE - 1).execute().data)
            out.extend(self._price_row(g) for g in got)
            if len(got) < _PAGE:
                return out
            start += _PAGE

    def similar(self, manufacturer: str, key: str) -> list[str]:
        got = (self._db.table("price_rows").select("model, match_key")
               .eq("manufacturer", manufacturer).eq("kind", "base")
               .like("match_key", f"{key[:3]}%").limit(_PAGE).execute().data)
        models = {g["match_key"]: g["model"] for g in got}
        return [models[k] for k in difflib.get_close_matches(key, list(models), n=5)]

    def starting_with(self, manufacturer: str, prefix: str, limit: int) -> list[PriceRow]:
        """Products whose model starts with `prefix`, nearest first, for suggestions."""
        self.books()
        got = (self._db.table("price_rows").select("*")
               .eq("manufacturer", manufacturer).eq("kind", "base")
               .like("match_key", f"{prefix}%")
               .order("match_key").limit(min(limit, _PAGE)).execute().data)
        return [self._price_row(g) for g in got]

    def _price_row(self, g: dict[str, Any]) -> PriceRow:
        return PriceRow(
            manufacturer=g["manufacturer"], model=g["model"], kind=g["kind"],
            list_price=float(g["list_price"]), price_text=g["price_text"],
            pdf_page=g["pdf_page"], printed_page=g["printed_page"], finish=g["finish"],
            size=g["size"], description=g["description"], note=g["note"],
            book=self._file_of.get(g["book_id"], ""))
