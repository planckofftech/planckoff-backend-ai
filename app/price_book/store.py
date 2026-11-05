"""Price books in Supabase: written by the loader script, read by the price lookup.

Reading a book again replaces it. The old book row is deleted first and its rows
go with it (on delete cascade), so a model never has two answers from two reads
of one file. The delete and the inserts are not one transaction: a loader that
dies half way leaves the book partly loaded, and the fix is to run it again.
"""

from __future__ import annotations

import difflib
from datetime import datetime, timezone
import logging
from typing import Any

from app.db.client import client
from app.price_book import makers
from app.price_book.book_map import BookMap, IndexEntry, PageEntry
from app.price_book.page_finder import Book as MappedBook
from app.price_book.lookup import Book
from app.price_book.rows import BookRead, PriceRow, match_key

log = logging.getLogger(__name__)

_BATCH = 500
# PostgREST returns at most this many rows per request, whatever was asked.
_PAGE = 1000


def _quote(value: Any) -> str:
    """One value for a PostgREST list, quoted so any character in it is taken literally."""
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _any_of(values: Any) -> str:
    """An `in` filter for these values. postgrest-py's in_() quotes a value only when it
    holds , : ( ) and never escapes a quote inside it, so an id like 4-1/2" matched
    nothing and its row was silently missed."""
    return "(" + ",".join(_quote(v) for v in values) + ")"


def _array(values: Any) -> str:
    """An array literal for an `ov` filter, quoted the same way."""
    return "{" + ",".join(_quote(v) for v in values) + "}"


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


def save_map(book_map: BookMap, sha256: str) -> None:
    """Store a book's map, replacing any earlier map of the same file."""
    db = client()
    name = book_map.file_name
    db.table("price_book_pages").delete().eq("file_name", name).execute()
    db.table("price_book_index").delete().eq("file_name", name).execute()
    pages = [{"file_name": name, "sha256": sha256, "pdf_page": p.pdf_page,
              "printed_page": p.printed_page, "heading": p.heading, "prices": p.prices,
              "models": p.models, "has_text": p.has_text} for p in book_map.pages]
    for i in range(0, len(pages), _BATCH):
        db.table("price_book_pages").insert(pages[i:i + _BATCH]).execute()
    index = [{"file_name": name, "model": e.model, "match_key": match_key(e.model),
              "printed_page": e.printed_page, "pdf_page": e.pdf_page,
              "index_page": e.index_page} for e in book_map.index]
    for i in range(0, len(index), _BATCH):
        db.table("price_book_index").insert(index[i:i + _BATCH]).execute()
    log.info("price book map %s: %d pages, %d index entries", name, len(pages), len(index))


def maker_codes() -> dict[str, makers.Code]:
    """The code table: the standard codes, under whatever Supabase holds.

    Read with the standard codes underneath, so a database the migration has
    not reached yet still knows HA is Hager.
    """
    codes = makers.standard_codes()
    try:
        got = client().table("manufacturer_codes").select("*").execute().data
    except Exception as exc:  # noqa: BLE001 - no table yet is not an outage
        log.warning("manufacturer_codes not read, using the standard codes: %s", exc)
        return codes
    for g in got:
        codes[g["code"]] = makers.Code(g["code"], g["manufacturer"], g["source"],
                                       g["verified"], g["note"], bool(g.get("no_book")))
    return codes


def set_code(code: makers.Code) -> None:
    """Add a code or replace what it says."""
    client().table("manufacturer_codes").upsert({
        "code": code.code, "manufacturer": code.manufacturer, "source": code.source,
        "verified": code.verified, "note": code.note, "no_book": code.no_book,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).execute()


def delete_code(code: str) -> bool:
    """Remove a code from the table; whether there was one. A standard code
    removed still reads as the standard one underneath (maker_codes)."""
    return bool(client().table("manufacturer_codes").delete().eq("code", code).execute().data)


def maps_print(file_names: list[str], models: list[str]) -> set[str]:
    """Which of these models the maps of these books list on a page."""
    if not file_names or not models:
        return set()
    got = (client().table("price_book_pages").select("models")
           .filter("file_name", "in", _any_of(file_names)).filter("models", "ov", _array(models)).execute().data)
    printed = {m for g in got for m in g["models"]}
    return {m for m in models if m in printed}


def who_prints(models: list[str]) -> dict[str, set[str]]:
    """For each model, the books whose maps list it on a page."""
    out: dict[str, set[str]] = {m: set() for m in models}
    if not models:
        return out
    got = (client().table("price_book_pages").select("file_name, models")
           .ov("models", models).limit(_PAGE).execute().data)
    for g in got:
        for m in set(g["models"]) & set(models):
            out[m].add(g["file_name"])
    return out


def book_maps(file_names: list[str]) -> list[MappedBook]:
    """The stored maps of these books, for step A. A book with no map is left out."""
    db = client()
    out = []
    for name in file_names:
        pages, start = [], 0
        while True:
            got = (db.table("price_book_pages").select("*").eq("file_name", name)
                   .order("pdf_page").range(start, start + _PAGE - 1).execute().data)
            pages.extend(PageEntry(g["pdf_page"], g["printed_page"], g["heading"], g["prices"],
                                   g["models"], g["has_text"]) for g in got)
            if len(got) < _PAGE:
                break
            start += _PAGE
        if not pages:
            continue
        index = (db.table("price_book_index").select("model, printed_page, pdf_page, index_page")
                 .eq("file_name", name).not_.is_("pdf_page", "null").limit(_PAGE * 5).execute().data)
        out.append(MappedBook(name, pages, [IndexEntry(g["model"], g["printed_page"], g["index_page"],
                                                       g["pdf_page"]) for g in index]))
    return out


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
        self._codes: dict[str, makers.Code] | None = None

    def codes(self) -> dict[str, makers.Code]:
        if self._codes is None:
            self._codes = maker_codes()
        return self._codes

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
                   .eq("manufacturer", manufacturer).filter("match_key", "in", _any_of(sorted(keys)))
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


# --------------------------------------------------------------------------- #
# prices read by AI, and the runs that read them
# --------------------------------------------------------------------------- #

def ai_prices_for(manufacturer: str, product_keys: list[str]) -> dict[str, dict[str, Any]]:
    """Stored AI prices of these products, by product key."""
    if not product_keys:
        return {}
    got = (client().table("ai_prices").select("*").eq("manufacturer", manufacturer)
           .filter("product_key", "in", _any_of(product_keys)).execute().data)
    return {g["product_key"]: g for g in got}


def save_ai_price(row: dict[str, Any]) -> dict[str, Any]:
    """Store a product's verdict, replacing an unverified one. A verified price
    is a person's word and is not overwritten by a later run."""
    db = client()
    found = (db.table("ai_prices").select("*").eq("manufacturer", row["manufacturer"])
             .eq("product_key", row["product_key"]).execute().data)
    if found and found[0]["verified"]:
        return found[0]
    row = {**row, "updated_at": datetime.now(timezone.utc).isoformat()}
    return db.table("ai_prices").upsert(row, on_conflict="manufacturer,product_key").execute().data[0]


def ai_price(price_id: str) -> dict[str, Any] | None:
    got = client().table("ai_prices").select("*").eq("id", price_id).execute().data
    return got[0] if got else None


def confirm_ai_price(price_id: str, list_price: float, note: str,
                     parts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    change: dict[str, Any] = {} if parts is None else {"parts": parts}
    return (client().table("ai_prices").update({
        **change, "list_price": list_price, "verified": True, "verified_note": note,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", price_id).execute().data[0])


def search_ai_prices(manufacturer: str, prefix: str, verified_only: bool, limit: int) -> list[dict[str, Any]]:
    q = (client().table("ai_prices").select("*").eq("manufacturer", manufacturer)
         .like("match_key", f"{prefix}%"))
    if verified_only:
        q = q.eq("verified", True)
    return q.order("match_key").limit(min(limit, _PAGE)).execute().data


def start_run(items: list[dict[str, Any]], project_id: str | None) -> str:
    return (client().table("ai_price_runs").insert({
        "project_id": project_id, "status": "running", "items": items,
    }).execute().data[0]["id"])


def finish_run(run_id: str, result: dict[str, Any] | None, error: str = "") -> None:
    client().table("ai_price_runs").update({
        "status": "failed" if error else "done", "result": result,
        "counts": (result or {}).get("counts"), "usd": (result or {}).get("usd", 0),
        "error": error[:1000], "finished_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", run_id).execute()


def run_of(run_id: str) -> dict[str, Any] | None:
    got = client().table("ai_price_runs").select("*").eq("id", run_id).execute().data
    return got[0] if got else None


def save_calls(calls: list, run_id: str | None, project_id: str | None) -> None:
    """Keep the AI calls a run made (call_log.Call), for the cost report."""
    from dataclasses import asdict

    rows = [{**asdict(c), "run_id": run_id, "project_id": project_id,
             "usd": round(c.usd, 6)} for c in calls]
    for i in range(0, len(rows), _BATCH):
        client().table("ai_price_calls").insert(rows[i:i + _BATCH]).execute()


def mapped_books() -> dict[str, list[str]]:
    """Each maker's books that have a map in Supabase: what pricing with AI reads.

    From the book registry (app/price_book/readers.BOOKS, file -> maker) and
    price_book_pages -- never from price_books or price_rows, which hold the
    prices code read before pricing moved to AI and can be cleared without
    the AI noticing.
    """
    from app.price_book.readers import BOOKS

    got = client().table("price_book_pages").select("file_name").eq("pdf_page", 1).execute().data
    mapped = {g["file_name"] for g in got}
    out: dict[str, list[str]] = {}
    for name, (maker, _) in BOOKS.items():
        if name in mapped:
            out.setdefault(maker, []).append(name)
    return out


def settled_ai_prices(manufacturer: str, prefix: str, limit: int) -> list[dict[str, Any]]:
    """AI prices with a price: agreed by two models, or confirmed by a person."""
    return (client().table("ai_prices").select("*").eq("manufacturer", manufacturer)
            .like("match_key", f"{prefix}%").not_.is_("list_price", "null")
            .order("verified", desc=True).order("match_key").limit(min(limit, _PAGE)).execute().data)


def guides(file_names: list[str]) -> dict[str, dict[str, Any]]:
    """The stored guides of these books, by file name (db/028_price_book_guides.sql)."""
    if not file_names:
        return {}
    got = client().table("price_book_guides").select("*").filter("file_name", "in", _any_of(file_names)).execute().data
    return {g["file_name"]: g for g in got}


def save_guide(file_name: str, sha256: str, guide: dict[str, Any], model: str, usd: float) -> dict[str, Any]:
    """Store a book's guide. One a person has reviewed is their word and is kept."""
    db = client()
    found = db.table("price_book_guides").select("*").eq("file_name", file_name).execute().data
    if found and found[0]["reviewed"]:
        return found[0]
    return db.table("price_book_guides").upsert({
        "file_name": file_name, "sha256": sha256, "guide": guide, "model": model, "usd": round(usd, 6),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, on_conflict="file_name").execute().data[0]


def queue_items(run_id: str, item_ids: list[str]) -> None:
    """One progress row per item of a run, queued (db/029_ai_price_agents.sql)."""
    # An id sent twice is one row: it is one item to the frontend.
    rows = [{"run_id": run_id, "item_id": i, "position": n, "status": "queued"}
            for n, i in enumerate(dict.fromkeys(item_ids))]
    for i in range(0, len(rows), _BATCH):
        client().table("ai_price_run_items").insert(rows[i:i + _BATCH]).execute()


def item_progress(run_id: str, item_ids: list[str | None], status: str, step: str = "",
                  answer: dict[str, Any] | None = None) -> None:
    """Move items of a run on: working with the step their agent is on, or done with the answer."""
    ids = [i for i in item_ids if i is not None]
    if not ids:
        return
    change: dict[str, Any] = {"status": status, "step": step[:200],
                              "updated_at": datetime.now(timezone.utc).isoformat()}
    if answer is not None:
        change["answer"] = answer
    client().table("ai_price_run_items").update(change).eq("run_id", run_id).filter("item_id", "in", _any_of(ids)).execute()


def run_items(run_id: str) -> list[dict[str, Any]]:
    """A run's items as they stand, in the order they were sent."""
    return (client().table("ai_price_run_items").select("item_id, position, status, step, answer, updated_at")
            .eq("run_id", run_id).order("position").execute().data)
