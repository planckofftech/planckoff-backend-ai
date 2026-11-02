"""Pages with a Description | Size | Finish | List | Box Qty. | Case Qty. table.

Hager's hinge pages (2026 price book, p. 25):

    Description          Size              Finish   List     Box  Case
    WTAB750              4-1/2" x 5"       LS       235.67   3    24
    Wide Throw ...       (114 x 127 mm)    USP      255.52
                                           ...
                                           US26D    237.16
                         4-1/2" x 6"       LS       236.80   3    24
                                           ...

One block per size, and each block lists the same finishes again -- so a finish
seen twice means the next size has begun. (The box quantity also opens a block,
but not every page prints one.) The size is printed part way down its block,
centred on it rather than on its first line, so sizes are matched to blocks by
position, not taken from the line above. The model code opens the first block
of its product and holds for the sizes after it, within the page: a product
carried over prints its code again with "Continued".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import fitz

from app.price_book.page_text import DECIMAL, MONEY, Piece, amount, count_prices, lines, pieces
from app.price_book.readers.common import effective_date, printed_page
from app.price_book.rows import BookRead, PriceRow, UnreadPage

NAME = "size_finish_grid"
_HEADER = ("Description", "Size", "Finish", "List")
_DATE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_MODEL = re.compile(r"^(?=.*\d)(?!\d{2}/\d{2}/\d{4})[A-Z0-9][A-Z0-9\-/.]{2,}$")
# '4-1/2" x 5"' for a hinge, '5-3/4"' (or '5-3/4') for a strike.
_SIZE = re.compile(r'^\d[\d\-/.]*"?(\s*x\s*\d[\d\-/.]*"?)?$')
# A size printed this far above its block's first line still belongs to it.
_SIZE_LEAD = 6


@dataclass
class _Block:
    y: float
    model: str
    entries: list[tuple[str, Piece]] = field(default_factory=list)   # (finish, price)

    def has(self, fin: str) -> bool:
        return any(f == fin for f, _ in self.entries)


def read(doc: fitz.Document, manufacturer: str, file_name: str) -> BookRead:
    book = BookRead(manufacturer, NAME, file_name, doc.page_count, effective_date(doc))
    carried: dict[str, str] = {}
    for index in range(doc.page_count):
        page = doc[index]
        items = pieces(page)
        on_page = count_prices(items)
        rows = lines(items)
        header = next((r for r in rows if _is_header(r)), None)
        if header is None:
            carried = {}
            if on_page:
                book.unread.append(UnreadPage(index + 1, on_page, "layout not read yet"))
            continue
        used, carried = _read_page(book, rows, header, index + 1,
                                   printed_page(page.get_text()), carried)
        book.prices_read += used
        if on_page > used:
            book.unread.append(UnreadPage(
                index + 1, on_page - used, "prices on this page outside the size/finish grid"))
    return book


def _is_header(line: list[Piece]) -> bool:
    texts = [p.text for p in line]
    return all(h in texts for h in _HEADER)


def _read_page(book: BookRead, rows: list[list[Piece]], header: list[Piece], pdf_page: int,
               printed: str, carried: dict[str, str]) -> tuple[int, dict[str, str]]:
    """Rows from one page; also each model's description, for a page that continues it.

    "BB1279 Continued" opens a page with no description of its own -- that was
    printed on the page before, and is only taken from there.
    """
    x = {p.text: p.x0 for p in header}
    size_x, finish_x, list_x = x["Size"], x["Finish"], x["List"]
    start = header[0].yc
    blocks: list[_Block] = []
    sizes: list[tuple[float, str]] = []
    described: dict[str, list[str]] = {}
    model = ""
    where = f"p.{pdf_page}"

    for line in rows:
        if line[0].yc <= start:
            continue
        desc = [p for p in line if p.x1 < size_x - 5]
        size = " ".join(p.text for p in line if size_x - 40 <= p.x0 < finish_x - 25)
        fin = " ".join(p.text for p in line if finish_x - 25 <= p.x0 < list_x - 5)
        price = next((p for p in line if list_x - 15 <= p.x0 < list_x + 40
                      and (DECIMAL.match(p.text) or MONEY.match(p.text))), None)
        qty = [p for p in line if p.x0 >= list_x + 40 and p.text.isdigit()]

        # One span can hold the code and its first words: "BB1191 Unequal Leaf".
        words = " ".join(p.text for p in desc).split()
        if any("www." in w or _DATE.match(w) for w in words):
            words = []                                  # the page footer
        if words and _MODEL.match(words[0]):
            model, words = words[0], words[1:]
            described.setdefault(model, [])
        if model and words and words != ["Continued"]:
            described[model].extend(words)
        size = size.split("(")[0].strip()
        if size and _SIZE.match(size):
            sizes.append((line[0].yc, size))
        if price is None:
            continue
        if not fin:
            book.rejected.append(f"{where}: {price.text} has no finish beside it")
            continue
        current = blocks[-1] if blocks else None
        if current is None or qty or current.model != model or current.has(fin):
            blocks.append(_Block(line[0].yc, model))
        blocks[-1].entries.append((fin, price))

    used = 0
    for i, block in enumerate(blocks):
        end = blocks[i + 1].y - _SIZE_LEAD if i + 1 < len(blocks) else float("inf")
        inside = [s for y, s in sizes if block.y - _SIZE_LEAD <= y < end]
        if len(inside) > 1:
            book.rejected.append(
                f"{where}: {block.model or '?'} block at y={block.y:.0f} has "
                f"{len(inside)} sizes beside it")
            continue
        if not block.model:
            book.rejected.append(f"{where}: block at y={block.y:.0f} has no model above it")
            continue
        # Accessories -- a screw pack, a mortar box -- print no size at all.
        size = _size_text(inside[0]) if inside else ""
        description = " ".join(described.get(block.model, [])) or carried.get(block.model, "")
        for fin, price in block.entries:
            used += 1
            notes = [n for n in (_alias_note(fin), "" if size else "No size printed") if n]
            book.rows.append(PriceRow(
                manufacturer=book.manufacturer, model=block.model, kind="base",
                list_price=amount(price.text), price_text=price.text, pdf_page=pdf_page,
                printed_page=printed, finish=_finish_text(fin), size=size,
                description=description, note="; ".join(notes)))
    text = {m: " ".join(w) or carried.get(m, "") for m, w in described.items()}
    return used, text


def _size_text(raw: str) -> str:
    """'4-1/2" x 5"' and '4-1/2 x 5' -> '4-1/2" x 5"'; '5-3/4' -> '5-3/4"'."""
    return " x ".join(f'{s.strip().rstrip(chr(34))}"' for s in raw.split("x"))


def _finish_text(fin: str) -> str:
    """'US10A (US11)' -> 'US10A'; the name in brackets is an older alias."""
    return fin.split("(")[0].strip()


def _alias_note(fin: str) -> str:
    m = re.search(r"\(([^)]+)\)", fin)
    return f"Also called {m.group(1)}" if m else ""
