"""Pages headed "PRICE BASED ON FINISH": one row per model, one column per finish.

Allegion's configurator pages (Von Duprin price book 16, p. 30):

    PRICE BASED ON FINISH
                  626    628    630   ...  605/619/625/643e  ...  613
    EXIT HARDWARE        3' DEVICE PRICING SHOWN. 2' ADD $118.00 LIST, 4' ADD $30.00 LIST.
    [98/99]48 . EO . [ ] . [ ]   $3,997 $3,621 $4,091 ...   N/A
    [98/99]48 . EO . F . [ ] ... $4,538 ...                  (F = fire rated)

A row spells its model in parts: the product, with alternatives in brackets
("[98/99]48" is 9848 or 9948), then the function, then "F" when fire rated.
The brackets after that are options chosen when ordering; they do not change
this price. A column can carry several finishes stacked one above another;
they share its price.

The size note above a group of rows says which device size the prices are for
and what another size adds. It holds until the next note.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import fitz

from app.price_book import finishes
from app.price_book.page_text import MONEY, Piece, amount, count_prices, lines, pieces
from app.price_book.readers.common import effective_date, printed_page
from app.price_book.rows import BookRead, PriceRow, UnreadPage

NAME = "finish_columns"
ANCHOR = "PRICE BASED ON FINISH"

# Left of this is the page's tab strip ("98/99 SERIES" printed sideways).
_MARGIN_X = 30
# A price this far from every finish column belongs to none of them.
_COLUMN_REACH = 15
# Header finishes this close in x are one column, stacked.
_STACK = 10

_PRODUCT = re.compile(r"^(?P<pre>[A-Z][A-Z\-.]*)?(?:\[(?P<alts>[0-9A-Z/]+)\])?(?P<suf>[0-9A-Z\-]*)$")
_FINISH = re.compile(r"^(\d{3}[A-Z]{0,2}|US\d+[A-Z]{0,3}|SP[A-Z0-9]+)$")
_SHOWN = re.compile(r"((?:\d' AND )?\d')\s+DEVICE PRICING SHOWN")
_ADD = re.compile(r"(\d')\s+ADD\s+(\$[\d,]+\.\d\d)")


@dataclass
class _Column:
    xc: float
    finishes: list[str] = field(default_factory=list)


@dataclass
class _SizeNote:
    priced: str                                   # "3'" or "3',4'"
    adds: list[tuple[str, str]]                   # [("4'", "$30.00")]


def expand(product: str) -> list[str]:
    """"[98/99]48" -> ["9848", "9948"]; "XP[98/99]" -> ["XP98", "XP99"]."""
    m = _PRODUCT.match(product)
    if not m or not any(ch.isdigit() for ch in product):
        return []
    pre, alts, suf = m.group("pre") or "", m.group("alts"), m.group("suf") or ""
    if alts is None:
        return [pre + suf]
    return [pre + a + suf for a in alts.split("/")]


def _size_note(text: str) -> _SizeNote | None:
    m = _SHOWN.search(text)
    if not m:
        return None
    priced = ",".join(re.findall(r"\d'", m.group(1)))
    return _SizeNote(priced, _ADD.findall(text))


def _finishes_in(text: str) -> list[str]:
    """Finish codes printed in a header piece, exactly as printed.

    "689 or 695" heads one column with two finishes. A code is taken by its
    shape, not from a list: p.127 heads a column "526", and the price under
    it is the price of what the page says, not of a finish we think it meant.
    """
    found = [finishes.norm(t) for t in re.split(r"\s+or\s+|/", text)]
    return found if found and all(_FINISH.match(f) for f in found) else []


def _add_column(cols: list[_Column], piece: Piece, found: list[str]) -> None:
    for col in cols:
        if abs(col.xc - piece.xc) <= _STACK:
            col.finishes.extend(found)
            return
    cols.append(_Column(piece.xc, list(found)))


def read(doc: fitz.Document, manufacturer: str, file_name: str) -> BookRead:
    book = BookRead(manufacturer, NAME, file_name, doc.page_count, effective_date(doc))
    for index in range(doc.page_count):
        page = doc[index]
        items = pieces(page)
        on_page = count_prices(items)
        if not any(ANCHOR in p.text for p in items):
            if on_page:
                book.unread.append(UnreadPage(index + 1, on_page, "layout not read yet"))
            continue
        used = _read_page(book, items, index + 1, printed_page(page.get_text()))
        book.prices_read += used
        if on_page > used:
            book.unread.append(UnreadPage(
                index + 1, on_page - used, "other tables on this page are not read yet"))
    return book


def _read_page(book: BookRead, items: list[Piece], pdf_page: int, printed: str) -> int:
    cols: list[_Column] = []
    collecting = False
    note: _SizeNote | None = None
    section = ""
    used = 0
    for line in lines(items):
        text = " ".join(p.text for p in line)
        if ANCHOR in text:
            cols, collecting = [], True
            continue
        found = _size_note(text)
        if found:
            note = found
            section = " ".join(p.text for p in line if "SHOWN" not in p.text).strip()
            continue
        money = [p for p in line if MONEY.match(p.text)]
        has_dot = any(p.text == "." for p in line)
        if collecting and not (money and has_dot):
            for p in line:
                found = _finishes_in(p.text)
                if found:
                    _add_column(cols, p, found)
            continue
        if not (money and has_dot):
            continue
        collecting = False
        used += _read_row(book, line, money, cols, note, section, pdf_page, printed)
    return used


def _read_row(book: BookRead, line: list[Piece], money: list[Piece], cols: list[_Column],
              note: _SizeNote | None, section: str, pdf_page: int, printed: str) -> int:
    first_x, last_x = money[0].x0, money[-1].x1
    left = [p.text for p in line if _MARGIN_X < p.x0 < first_x]
    trim = " ".join(p.text for p in line if p.x0 > last_x)
    where = f"p.{pdf_page}"
    if not cols:
        book.rejected.append(f"{where}: prices with no finish header above: {' '.join(left)}")
        return 0
    if len(left) < 3 or left[1] != ".":
        book.rejected.append(f"{where}: row does not start 'product . function': {' '.join(left)}")
        return 0
    products = expand(left[0])
    function = left[2]
    if not products or function == "." or function.startswith("["):
        book.rejected.append(f"{where}: cannot spell the model from: {' '.join(left)}")
        return 0
    fire = "F" in left[3:]
    models = [f"{p}{function}{'-F' if fire else ''}" for p in products]

    priced = note.priced if note else ""
    notes = []
    if trim and trim != "N/A":
        notes.append(f"Default trim included: {trim}")
    if not note:
        notes.append("No device size stated above this row")
    description = (section + (" (fire rated)" if fire else "")).strip()

    used = 0
    for price in money:
        col = min(cols, key=lambda c: abs(c.xc - price.xc))
        if abs(col.xc - price.xc) > _COLUMN_REACH:
            book.rejected.append(f"{where}: {price.text} for {models[0]} sits under no finish column")
            continue
        used += 1
        for model in models:
            for finish in col.finishes:
                book.rows.append(PriceRow(
                    manufacturer=book.manufacturer, model=model, kind="base",
                    list_price=amount(price.text), price_text=price.text,
                    pdf_page=pdf_page, printed_page=printed, finish=finish,
                    size=priced, description=description, note="; ".join(notes)))
    if note:
        for model in models:
            for size, add in note.adds:
                book.rows.append(PriceRow(
                    manufacturer=book.manufacturer, model=model, kind="size_add",
                    list_price=amount(add), price_text=add, pdf_page=pdf_page,
                    printed_page=printed, size=size,
                    description=f"{size} device instead of {priced}",
                    note=f"Page note: {priced} device pricing shown, {size} add {add} list"))
    return used
