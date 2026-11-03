"""A hardware item in; its list price, the parts it adds up to, and their pages out.

The answer is one of four, and only the first carries a price that can be used
as it stands:

  priced           every part was found, once, with nothing assumed
  needs_review     something is missing or appears more than once. Where one
                   reading is the book's own default (a device priced at 3'
                   because no length was given) that price is returned with the
                   reason; where the book prints several candidates, they are
                   returned as options and no price is chosen
  not_found        the book was read but has no such model or finish
  book_not_loaded  no book for this manufacturer has been read yet

Nothing here estimates. A missing price is reported as missing -- the frontend
used to show $0, which reads as "free" rather than "unknown".
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Literal, Protocol

from app.price_book import finishes, makers, sizes
from app.price_book.rows import PriceRow, match_key

Status = Literal["priced", "needs_review", "not_found", "book_not_loaded"]

# Bracketed notes a schedule adds after the item: '[10" x 34"]', '[36"]'.
_BRACKETED = re.compile(r"\[[^\]]*\]")


@dataclass
class Item:
    manufacturer: str
    model: str = ""
    finish: str = ""
    size: str = ""
    description: str = ""
    quantity: float = 1


@dataclass
class Part:
    label: str
    list_price: float
    pdf_page: int
    printed_page: str
    book: str


@dataclass
class Option:
    """One of several rows that could be meant; a person picks."""

    model: str
    size: str
    finish: str
    list_price: float
    description: str
    pdf_page: int
    printed_page: str
    book: str


@dataclass
class Quote:
    status: Status
    manufacturer: str = ""
    model: str = ""
    finish: str = ""
    size: str = ""
    list_price: float | None = None
    extended_price: float | None = None
    parts: list[Part] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    options: list[Option] = field(default_factory=list)
    book: str = ""
    effective_date: str | None = None


@dataclass
class Book:
    manufacturer: str
    file_name: str
    effective_date: str | None


class Source(Protocol):
    """Where rows come from: the database, or books just read into memory."""

    def books(self) -> dict[str, Book]: ...                     # by file name
    def rows(self, manufacturer: str, keys: set[str]) -> list[PriceRow]: ...
    def similar(self, manufacturer: str, key: str) -> list[str]: ...
    def codes(self) -> dict[str, makers.Code]: ...              # the maker-code table


def manufacturer_of(name: str, loaded: set[str],
                    codes: dict[str, makers.Code] | None = None) -> str | None:
    """The loaded maker a schedule's manufacturer means, or None."""
    maker, _, _ = makers.resolve(name, loaded, makers.standard_codes() if codes is None else codes)
    return maker if maker in loaded else None


def _not_loaded(name: str, codes: dict[str, makers.Code]) -> str:
    maker, _, _ = makers.resolve(name, set(), codes)
    named = f"'{name}' ({maker})" if maker and match_key(maker) != match_key(name) else f"'{name}'"
    return f"No price book for {named} has been loaded"


def _spans(text: str) -> list[str]:
    """Keys of every run of consecutive words: "9948 EO F" -> 9948, 9948EO, ..."""
    words = [w for w in re.split(r"[\s,;]+", text) if w]
    out = []
    for i in range(len(words)):
        for j in range(i + 1, min(len(words), i + 5) + 1):
            out.append(match_key("".join(words[i:j])))
    return [k for k in out if len(k) >= 2]


def quote(item: Item, source: Source) -> Quote:
    loaded = source.books()
    codes = source.codes()
    maker = manufacturer_of(item.manufacturer, {b.manufacturer for b in loaded.values()}, codes)
    if maker is None:
        return Quote("book_not_loaded", manufacturer=item.manufacturer,
                     reasons=[_not_loaded(item.manufacturer, codes)])
    q = Quote("priced", manufacturer=maker, finish=item.finish)

    # The model may carry its size, finish or maker with it ("9948EO-F 4' 626").
    # With no model number, the schedule's description usually starts with one:
    # 'BB1168 4 1/2" x 4" NRP'.
    model_text = sizes.strip_sizes(_BRACKETED.sub(" ", item.model))
    if not _spans(model_text):
        model_text = sizes.strip_sizes(_BRACKETED.sub(" ", item.description))
    tried = set(_spans(model_text))
    if not tried:
        q.status = "not_found"
        q.reasons.append("No model number given")
        return q
    found = source.rows(maker, tried)
    keys = {r.key for r in found if r.kind == "base"}
    if not keys:
        q.status = "not_found"
        q.reasons.append(f"'{item.model or item.description}' is not on the {maker} "
                         "price book pages read so far")
        close = source.similar(maker, max(tried, key=len))
        if close:
            q.reasons.append("Closest models in the book: " + ", ".join(close))
        return q
    key = max(keys, key=len)          # "9948EOF" over "9948EO" when F was written
    rows = [r for r in found if r.key == key]
    base = [r for r in rows if r.kind == "base"]
    q.model = base[0].model

    base = _by_finish(q, base, item.finish, f"{item.model} {item.description}")
    if not base:
        return q
    base, extra = _by_size(q, base, rows, item)
    if not base:
        return q

    distinct = {(r.list_price, r.book, r.pdf_page, r.description, r.size) for r in base}
    if len(distinct) > 1:
        q.status = "needs_review"
        q.reasons.append(
            f"{q.model} in {q.finish or 'this finish'} is printed more than once "
            "with different prices; choose one")
        q.options = [Option(r.model, r.size, r.finish, r.list_price, r.description,
                            r.pdf_page, r.printed_page, r.book) for r in base]
        return q

    chosen = base[0]
    q.size = q.size or chosen.size
    q.book = chosen.book
    q.effective_date = loaded[chosen.book].effective_date if chosen.book in loaded else None
    q.parts.append(Part(_label(chosen), chosen.list_price, chosen.pdf_page,
                        chosen.printed_page, chosen.book))
    q.parts.extend(Part(_label(r), r.list_price, r.pdf_page, r.printed_page, r.book)
                   for r in extra)
    q.list_price = round(sum(p.list_price for p in q.parts), 2)
    q.extended_price = round(q.list_price * item.quantity, 2)

    # A word left over may be an option with a price of its own -- NRP is $7.02
    # a hinge in Hager's book -- so the price stands but is not taken as whole.
    left = _left_over(model_text, key, q.finish)
    if left:
        q.status = "needs_review"
        q.reasons.append(f"{', '.join(left)} not priced; check whether it adds to "
                         f"the {q.model} price")
    return q


def _left_over(text: str, key: str, finish: str) -> list[str]:
    """Words of the text that are not the model, its finish, or an 'x' between."""
    words = [w for w in re.split(r"[\s,;]+", text) if match_key(w)]
    for i in range(len(words)):
        for j in range(i + 1, min(len(words), i + 5) + 1):
            if match_key("".join(words[i:j])) == key:
                words = words[:i] + words[j:]
                break
        else:
            continue
        break
    same = finishes.same(finish) if finish else set()
    return [w for w in words
            if match_key(w) != "X" and not (same and finishes.norm(w) in same)]


def _label(row: PriceRow) -> str:
    if row.kind == "size_add":
        return f"{row.size} size add-on"
    bits = [row.model, row.finish, row.size]
    return " ".join(b for b in bits if b)


def _by_finish(q: Quote, base: list[PriceRow], finish: str, text: str) -> list[PriceRow]:
    printed = sorted({r.finish for r in base if r.finish})
    if not printed:
        return base                          # priced the same in every finish
    if not finish:
        # Written into the model instead: "9948EO-F 4' 626". Only a word that
        # is one of this model's printed finishes counts.
        names = {finishes.norm(f) for f in printed}
        named = {w for w in re.split(r"[\s,;]+", text) if w and finishes.same(w) & names}
        if len(named) == 1:
            finish = named.pop()
            q.finish = finish
    if not finish:
        q.status = "needs_review"
        q.reasons.append(f"Finish not given; {q.model} is priced by finish: {', '.join(printed)}")
        return []
    wanted = finishes.same(finish)
    kept = [r for r in base if finishes.norm(r.finish) in wanted]
    if not kept:
        q.status = "not_found"
        q.reasons.append(f"Finish {finish} is not priced for {q.model}; "
                         f"the book prices: {', '.join(printed)}")
    return kept


def _by_size(q: Quote, base: list[PriceRow], rows: list[PriceRow],
             item: Item) -> tuple[list[PriceRow], list[PriceRow]]:
    """The base rows for the size asked for, and any size add-on it needs."""
    adds = [r for r in rows if r.kind == "size_add"]
    printed = sorted({r.size for r in base if r.size})
    if not printed:
        return base, []
    text = " ".join(t for t in (item.size, item.model, item.description) if t)

    if adds or all(s.endswith("'") for p in printed for s in p.split(",")):
        return _by_length(q, base, adds, printed, text)

    wanted = sizes.dimensions(item.size) if item.size else None
    if wanted is None:
        m = re.search(r"\d[\d\s./-]*\"?\s*[xX×]\s*\d[\d\s./-]*\"?", text)
        wanted = sizes.dimensions(m.group(0)) if m else None
    if wanted is None:
        if len(printed) == 1 and sizes.dimensions(printed[0]) is not None:
            return base, []
        if len(printed) == 1:
            # 'Under 8" high': the price holds only for plates that size.
            q.status = "needs_review"
            q.reasons.append(f"Size not given; {q.model} is priced for {printed[0]}")
            q.size = printed[0]
            return base, []
        q.status = "needs_review"
        q.reasons.append(f"Size not given; {q.model} is priced by size: {'; '.join(printed)}")
        return [], []
    kept = [r for r in base if sizes.dimensions(r.size) == wanted]
    if not kept:
        q.status = "not_found"
        q.reasons.append(f"Size {item.size or text} is not priced for {q.model}; "
                         f"the book prices: {'; '.join(printed)}")
    q.size = kept[0].size if kept else ""
    return kept, []


def _by_length(q: Quote, base: list[PriceRow], adds: list[PriceRow], printed: list[str],
               text: str) -> tuple[list[PriceRow], list[PriceRow]]:
    """Exit devices: priced at one length, other lengths by a printed add-on."""
    covered = {s for p in printed for s in p.split(",")}
    want = sizes.device_length(text)
    if want is None:
        q.status = "needs_review"
        q.reasons.append(f"Device size not given; the book's price is for "
                         f"{' and '.join(sorted(covered))}, which this price assumes")
        q.size = ",".join(sorted(covered))
        return base, []
    q.size = want
    if want in covered:
        return base, []
    extra = [r for r in adds if r.size == want]
    if not extra:
        q.status = "not_found"
        q.reasons.append(f"No price for a {want} {q.model}; the book prices "
                         f"{', '.join(sorted(covered | {r.size for r in adds}))}")
        return [], []
    # One add-on per length. The same note can be read off more than one page,
    # and the add-on must come from the page the base price did.
    extra = [r for r in extra if r.book == base[0].book and r.pdf_page == base[0].pdf_page] \
        or extra
    amounts = {r.list_price for r in extra}
    if len(amounts) > 1:
        q.status = "needs_review"
        q.reasons.append(f"The {want} add-on for {q.model} is printed with different amounts")
        return [], []
    return base, extra[:1]


class MemorySource:
    """Rows held in memory: books just read, or rows built in a test."""

    def __init__(self, books: list[Book], rows: list[PriceRow]):
        self._books = {b.file_name: b for b in books}
        self._by_key: dict[tuple[str, str], list[PriceRow]] = {}
        for r in rows:
            self._by_key.setdefault((r.manufacturer, r.key), []).append(r)

    def books(self) -> dict[str, Book]:
        return self._books

    def codes(self) -> dict[str, makers.Code]:
        return makers.standard_codes()

    def rows(self, manufacturer: str, keys: set[str]) -> list[PriceRow]:
        return [r for k in keys for r in self._by_key.get((manufacturer, k), [])]

    def starting_with(self, manufacturer: str, prefix: str, limit: int) -> list[PriceRow]:
        found = [r for (m, k), rs in self._by_key.items()
                 if m == manufacturer and k.startswith(prefix) for r in rs if r.kind == "base"]
        return sorted(found, key=lambda r: r.key)[:limit]

    def similar(self, manufacturer: str, key: str) -> list[str]:
        models = {r.key: r.model for (m, _), rs in self._by_key.items()
                  if m == manufacturer for r in rs}
        return [models[k] for k in difflib.get_close_matches(key, list(models), n=5)]
