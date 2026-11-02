"""Suggestions while someone types a model number: "BB11", "BB1168 US26D", "9948 626".

The words typed are read as a model, with a finish and a size where one is
written: a finish is any word the finish table knows (626, US26D, 26D), a size
any hinge or device size (4-1/2 x 4, 4'). The model is matched from its start,
punctuation aside -- "4050AT-30" finds 4050AT-3038H -- so each key typed narrows
the list. Only products are suggested; option add-ons are left out.

What comes back is rows as printed: one per model, finish and size, with its
price and page. Nothing is combined or worked out here; that is `lookup.quote`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from app.price_book import finishes, sizes
from app.price_book.rows import PriceRow, match_key

# A model shorter than this matches too much to be worth a request.
MIN_KEY = 2


@dataclass
class Query:
    key: str                                        # the model typed, as a match key
    finish: str = ""                                # as typed
    finish_names: set[str] = field(default_factory=set)
    size: tuple[float, ...] | None = None


@dataclass
class Suggestion:
    model: str
    finish: str
    size: str
    description: str
    list_price: float
    book: str
    pdf_page: int
    printed_page: str


class Searchable(Protocol):
    def starting_with(self, manufacturer: str, prefix: str, limit: int) -> list[PriceRow]: ...


def read_query(text: str) -> Query:
    """'BB1168 4 1/2 x 4 US26D' -> model BB1168, finish US26D, size 4.5 x 4."""
    size = None
    m = re.search(r"\d[\d\s./-]*\"?\s*[xX×]\s*\d[\d\s./-]*\"?", text)
    if m:
        size = sizes.dimensions(m.group(0))
    rest = sizes.strip_sizes(text)
    words = [w for w in re.split(r"[\s,;]+", rest) if w]
    finish = ""
    model: list[str] = []
    for w in words:
        # A finish only after the model has begun: "626" alone is a model typed.
        if model and not finish and _is_finish(w):
            finish = w
        else:
            model.append(w)
    return Query(match_key("".join(model)), finish, _names(finish) if finish else set(), size)


def suggest(source: Searchable, manufacturer: str, text: str, limit: int) -> list[Suggestion]:
    q = read_query(text)
    if len(q.key) < MIN_KEY:
        return []
    # Fetched wide, then narrowed: a finish or size typed can rule out most of
    # what starts with the model.
    rows = source.starting_with(manufacturer, q.key, limit * 25 if q.finish or q.size else limit * 4)
    kept = [r for r in rows if r.kind == "base"
            and (not q.finish_names or finishes.norm(r.finish) in q.finish_names)
            and (q.size is None or sizes.dimensions(r.size) == q.size)]
    kept.sort(key=lambda r: (r.key != q.key, len(r.key), r.key, r.finish, r.size, r.list_price))
    out: list[Suggestion] = []
    seen: set[tuple] = set()
    for r in kept:
        one = (r.key, r.finish, r.size, r.list_price, r.description)
        if one in seen:
            continue
        seen.add(one)
        out.append(Suggestion(r.model, r.finish, r.size, r.description, r.list_price,
                              r.book, r.pdf_page, r.printed_page))
        if len(out) == limit:
            break
    return out


def _is_finish(word: str) -> bool:
    return bool(_names(word) & finishes.KNOWN) or bool(re.match(r"^(6\d\d|7[0-2]\d)[A-Za-z]{0,2}$", word))


def _names(word: str) -> set[str]:
    """Every name the finish goes by: '26D' -> US26D and 626."""
    names = set(finishes.same(word)) | {finishes.norm(word)}
    if re.match(r"^\d{1,2}[A-Z]{1,3}$", word.upper()):
        names |= finishes.same("US" + word)
    return names
