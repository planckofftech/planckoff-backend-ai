"""What a price book is read into."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

# base        the price of the item in one finish (and one size, where sized)
# size_add    added to a base price for a size other than the one priced
# option_add  added for an option chosen on top of the item
Kind = Literal["base", "size_add", "option_add"]

_NOT_KEY = re.compile(r"[^A-Z0-9]")


def match_key(text: str) -> str:
    """A model number with the punctuation that spec writers vary removed.

    "9948EO-F", "9948 EO F" and "9948eo-f" are the same device. Only letters
    and digits survive, upper-cased; nothing is dropped beyond that, so a
    fire-rated device never matches its non-rated twin.
    """
    return _NOT_KEY.sub("", text.upper())


@dataclass
class PriceRow:
    manufacturer: str
    model: str                  # as the book builds it, e.g. "9948EO-F"
    kind: Kind
    list_price: float
    price_text: str             # exactly as printed, e.g. "$3,997"
    pdf_page: int               # 1-based, as a PDF viewer counts
    printed_page: str = ""      # the number printed on the page itself
    finish: str = ""            # "" when the price does not depend on finish
    size: str = ""              # the size priced, or added for a size_add
    description: str = ""
    note: str = ""
    book: str = ""              # the file it was read from; a maker can have several

    @property
    def key(self) -> str:
        return match_key(self.model)


@dataclass
class UnreadPage:
    """A page that prints prices no reader took. The gap, made visible."""

    pdf_page: int
    prices_on_page: int
    reason: str


@dataclass
class BookRead:
    manufacturer: str
    reader: str
    file_name: str
    page_count: int
    effective_date: str | None = None     # ISO date, when the book states one
    rows: list[PriceRow] = field(default_factory=list)
    # Prices as printed that became rows. One printed price can be several
    # rows (9848 and 9948 share one), so this, not len(rows), is what compares
    # with the prices left unread.
    prices_read: int = 0
    unread: list[UnreadPage] = field(default_factory=list)
    # Rows a reader saw but would not keep, each with why: a price that did
    # not sit under any finish column, a product it could not spell out.
    rejected: list[str] = field(default_factory=list)
