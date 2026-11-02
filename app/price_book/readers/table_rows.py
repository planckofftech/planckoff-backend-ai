"""Any price table whose rows name their model: one reader for the many layouts left.

Most books print a table some way like these:

    Product Number           List Price       (Pemko p. 30: three side by side)
    PEMKOSTCSET-1A36x80BL    $230.77

    Part number    Finish     Handing   Powder coat     (LCN p. 50)
    4050AT-3038H   .[Finish]  -         $164.00

    Family   OA Size   605/606   609   ...   630          (Trimco p. 40)
    AP751    12"       663.40    663.40    ...  414.60
             18"       862.40    ...

Every price is read the same way. Its column heading, the nearest text above it
that it sits under, says what the price is for: a finish ("605/606" is two
finishes at one price), a size, or a variant ("Powder coat"); "List" or "Price"
says nothing more. Its model is the code printed to its left on its own line,
past any size or finish there. A line with no code belongs to a block: the lines
under one code, or -- where the code is printed beside the middle of its lines,
as Trimco does -- the lines it sits beside. A block opens at a line with a code,
or at a line whose size and finish repeat one already in the block.

Nothing is guessed. A block with no code beside it, or with two, is not read; nor
is a page that prints one model, finish and size at two prices. Prices worded
into text ("Add $126.00 per cylinder") are left for a reader of option tables.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import fitz

from app.price_book import finishes
from app.price_book.page_text import DECIMAL, MONEY, Piece, amount, count_prices, lines, pieces
from app.price_book.readers.common import effective_date, printed_page
from app.price_book.rows import BookRead, PriceRow, UnreadPage

NAME = "table_rows"

# A code: at least one digit, no spaces. Lower case only after the first
# character -- Pemko prints "PEMKOSTCSET-1A36x80BL".
_CODE = re.compile(r"^(?=.*\d)[A-Z0-9][A-Za-z0-9\-./#]{2,}$")
# A finish as a column heading prints it: "612E", "SP28", "US10B".
_FINISH_HEAD = re.compile(r"^(\d{3}[A-Z]{0,2}|US\d+[A-Z]{0,3}|SP[A-Z0-9]+)$")
# A price worded into text: "Add $126.00 per cylinder", "$140.00/ cyl."
_MONEY_IN = re.compile(r"\$\d")
# "1", "10", "3†": an option's code, at the start of its row.
_SHORT_TAG = re.compile(r"^[0-9A-Z]{1,4}[†*]?$")
# Printed where a price would be, on a row of data rather than a heading.
_NO_PRICE = re.compile(r"\b(no (addition|charge)|n/a)\b", re.I)
_DATE = re.compile(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$")
# 12"   4-1/2" x 5"   3'   36" x 84"   1-3/4" -- a unit or an x is required.
_SIZE = re.compile(r"""^\d[\d\-/.]*\s*["']\s*(x\s*\d[\d\-/.]*\s*["']?)?$|^\d[\d\-/.]*\s*x\s*\d[\d\-/.]*["']?$""")
_PLAIN = {"LIST", "PRICE", "LISTPRICE", "LIST$", "PRICEEACH", "EACH", "EA", "MSRP", "NET"}
# A heading in these words is a measure, not a price.
_MEASURE = re.compile(r"\b(mm|lbs?|weight|wt|dia|qty|quantity|pack|box|case|carton)\b", re.I)
_OPTION = re.compile(r"\b(option|options|add|adder|deduct)\b", re.I)
# How far up a heading may be looked for, in printed lines: a long table
# prints its heading once, at the top.
_LOOK_UP = 400
# A label beside its block sits within this of the block's left edge.
_LABEL_REACH = 20
# A label may be printed this far above its block's first line.
_LEAD = 6


# A size worded into a short phrase: 'Under 8" high', 'Over 36" wide'.
_SIZE_WORDED = re.compile(r"""^(under|over|up to|to|from|max\.?|min\.?)\s+\d[\d\-/.]*\s*["']""", re.I)
# A heading row's words, in the code column or along it.
_HEAD_WORDS = re.compile(r"^(description|price|list|part|model|finish|size|item|catalog|product)\b", re.I)


def _breaks(row: list[Piece]) -> bool:
    """A line that ends the rows under a code: another code, a title, a heading."""
    first = row[0].text
    return bool(codes_in(first) or _SHORT_TAG.match(first) or first.endswith(":")
                or (re.search(r"[A-Z]{2}", first) and first.upper() == first)
                or any(_HEAD_WORDS.match(p.text) for p in row))


def is_price(p: Piece) -> bool:
    return bool(MONEY.match(p.text) or DECIMAL.match(p.text))


def is_code(text: str) -> bool:
    return bool(_CODE.match(text)) and not _DATE.match(text) and not DECIMAL.match(text) \
        and not is_finish(text)


def codes_in(text: str) -> list[str]:
    """'4050AT-72' -> ['4050AT-72']; '101H, 102H' -> both; [] unless all are codes."""
    parts = [t for t in re.split(r",\s*", text) if t]
    return parts if parts and all(is_code(t) for t in parts) else []


def is_size(text: str) -> bool:
    return bool(_SIZE.match(text))


def is_finish(text: str) -> bool:
    """'626', 'US26D', and '26D' -- McKinney drops the US.

    Every BHMA number from 600 to 729 is a finish, listed in finishes.py or not
    (627, 652): a model numbered so is then not found, never priced as another.
    """
    return finishes.norm(text) in finishes.KNOWN or bool(re.match(r"^US\d+[A-Z]{0,3}$", text)) \
        or bool(re.match(r"^(6\d\d|7[0-2]\d)[A-Za-z]{0,2}$", text)) \
        or (bool(re.match(r"^\d{1,2}[A-Z]{1,3}$", text)) and finishes.norm("US" + text) in finishes.KNOWN)


@dataclass
class _Heading:
    finishes: list[str] = field(default_factory=list)
    size: str = ""
    variant: str = ""
    option: bool = False
    measure: bool = False


@dataclass
class _Line:
    y: float
    pieces: list[Piece]
    prices: list[tuple[Piece, list[Piece]]]       # (price, codes printed for it)
    size: str
    finish: str
    words: list[str]
    tagged: bool = False            # opens with a code too short to read as one
    named: bool = False             # opens with a name that is not read as a code


@dataclass
class _Block:
    y: float
    codes: list[Piece]
    lines: list[_Line] = field(default_factory=list)
    closed: bool = False            # its lines carry codes of their own, unread

    def has(self, line: _Line) -> bool:
        return bool(line.size or line.finish) and any(
            (l.size, l.finish) == (line.size, line.finish) for l in self.lines)


def read(doc: fitz.Document, manufacturer: str, file_name: str,
         pages: set[int] | None = None) -> BookRead:
    """Read every page, or only `pages` (1-based) when another reader took the rest."""
    book = BookRead(manufacturer, NAME, file_name, doc.page_count, effective_date(doc))
    for index in range(doc.page_count):
        if pages is not None and index + 1 not in pages:
            continue
        page = doc[index]
        items = pieces(page)
        on_page = count_prices(items)
        if not on_page:
            continue
        used = _read_page(book, lines(items), index + 1, printed_page(page.get_text()))
        book.prices_read += used
        if on_page > used:
            book.unread.append(UnreadPage(
                index + 1, on_page - used,
                "layout not read yet" if not used else "prices on this page not read yet"))
    return book


def _heading(rows: list[list[Piece]], i: int, piece: Piece) -> str:
    """The nearest text above `piece` that it sits under, skipping lines of prices."""
    for line in reversed(rows[max(0, i - _LOOK_UP):i]):
        if any(is_price(p) or _NO_PRICE.search(p.text) or _MONEY_IN.search(p.text) for p in line):
            continue
        over = [p for p in line if p.x0 - 4 <= piece.xc <= p.x1 + 4]
        text = " ".join(p.text for p in over)
        if over and (is_code(text) or _SHORT_TAG.match(text)) and not _finish_list(text):
            continue                                # a row of codes, not a heading
        if over:
            return text
    return ""


def _finish_list(text: str) -> list[str]:
    """'605/606' -> ['605', '606']; [] unless every word is a finish."""
    words = [w for w in re.split(r"\s*(?:/|\bor\b|,)\s*|\s+", text) if w]
    return words if words and all(_FINISH_HEAD.match(w) for w in words) else []


def _read_heading(text: str) -> _Heading:
    h = _Heading()
    if not text:
        return h
    if _MEASURE.search(text):
        h.measure = True
        return h
    # A heading is a few words; a note that mentions "add" is not one.
    h.option = bool(_OPTION.search(text)) and len(text.split()) <= 4
    if _finish_list(text):
        h.finishes = _finish_list(text)
    elif is_size(text):
        h.size = text
    elif re.sub(r"[^A-Z$]", "", text.upper()) not in _PLAIN:
        h.variant = text
    return h


def _read_line(rows: list[list[Piece]], i: int) -> _Line:
    prices: list[tuple[Piece, list[Piece]]] = []
    size = finish = ""
    words: list[str] = []
    codes: list[Piece] = []
    for p in rows[i]:
        if is_price(p):
            prices.append((p, codes))
            codes = []                  # the next price takes only codes printed after this one
        elif is_size(p.text) or (_SIZE_WORDED.search(p.text) and not prices):
            # 'Under 8" high' is the size a kick plate's price holds for, as
            # much as '10" x 34"' is: kept as printed, so it is never taken
            # for another size.
            size = size or p.text
        elif is_finish(p.text):
            finish = finish or p.text
        elif codes_in(p.text):
            # A catalog number beside a part number: the price is under either.
            codes.append(p)
        elif re.search(r"[A-Za-z]{2}", p.text) and p.text[0] not in ".[" and not prices:
            words.append(p.text)
    # A line of finish columns prints its code once, before the first price.
    first = prices[0][1] if prices else []
    prices = [(p, c or first) for p, c in prices]
    head = rows[i][0]
    tagged = len(rows[i]) > 1 and bool(_SHORT_TAG.match(head.text))
    # "07 B15", "5BB1 4.5x4.5": the row names itself, in a way not read as a code.
    # A description line ('Fastener: #10 x 1-1/2" PPH') is longer than that.
    named = len(head.text.split()) <= 2 and bool(re.search(r"\d", head.text.split()[0])) \
        and not is_price(head) and not codes_in(head.text) \
        and not is_size(head.text) and not is_finish(head.text)
    return _Line(head.yc, rows[i], prices, size, finish, words, tagged, named)


def _read_page(book: BookRead, rows: list[list[Piece]], pdf_page: int, printed: str) -> int:
    where = f"p.{pdf_page}"
    blocks: list[_Block] = []
    labels: list[tuple[Piece, list[str], int]] = []          # code, words with it, line
    headings: dict[int, _Heading] = {}
    for i, row in enumerate(rows):
        if not any(is_price(p) for p in row):
            codes = [p for p in row if codes_in(p.text)]
            if len(codes) == 1:
                rest = [p.text for p in row if p is not codes[0] and re.search(r"[A-Za-z]{2}", p.text)]
                labels.append((codes[0], rest, i))
            current = blocks[-1] if blocks else None
            if current and current.codes and not current.closed \
                    and abs(row[0].x0 - current.lines[0].pieces[0].x0) <= _LABEL_REACH \
                    and _breaks(row):
                # The next code, a section title or a heading row in the code
                # column ends the rows under the code above. A line of its
                # description ("cylinders also available.") does not.
                blocks.append(_Block(row[0].yc, [], closed=True))
            continue
        line = _read_line(rows, i)
        for p, cs in line.prices:
            headings[id(p)] = _read_heading(_heading(rows, i, p))
            for c in cs:
                headings.setdefault(id(c), _read_heading(_heading(rows, i, c)))
        own = line.prices[0][1]
        current = blocks[-1] if blocks else None
        # Only in the code column: "US4" or "BLK" opening a row further right
        # is that row's finish.
        in_column = current is None or not current.lines or \
            abs(line.pieces[0].x0 - current.lines[0].pieces[0].x0) <= _LABEL_REACH
        if not own and in_column and (line.tagged or (line.named and current is not None
                                                      and current.codes)):
            # "1  8200 Line with 1-1/16" front   $94.00" -- an option's own code,
            # too short to tell from a footnote -- or a row naming itself some
            # other way. Never lent the code of the row above.
            blocks.append(_Block(line.y, [], closed=True))
            continue
        if current is None or own or current.has(line):
            blocks.append(_Block(line.y, own))
        blocks[-1].lines.append(line)

    left = min((l.pieces[0].x0 for b in blocks for l in b.lines), default=0)
    out: list[tuple[PriceRow, Piece]] = []
    for k, block in enumerate(blocks):
        if block.closed:
            continue
        codes = block.codes
        words = list(block.lines[0].words)
        if not codes:
            # Beside its lines: not above the first, nor below the last.
            end = block.lines[-1].y + _LEAD
            beside = [(c, w, i) for c, w, i in labels
                      if block.y - _LEAD <= c.yc < end and abs(c.x0 - left) <= _LABEL_REACH]
            if len(beside) != 1:
                continue
            code, more, at = beside[0]
            codes = [code]
            words = more + words
            headings[id(code)] = _read_heading(_heading(rows, at, code))
        description = " ".join(words)
        for line in block.lines:
            for price, own in line.prices:
                h = headings[id(price)]
                if h.measure:
                    continue
                # A row under the block's line takes its first code only: a
                # catalog number beside it belongs to that one line.
                names = own or codes[:1]
                # An "Options" column of codes lists add-ons, not products.
                option = h.option or any(headings.get(id(c), _Heading()).option for c in names)
                text = f"{description} [{h.variant}]".strip() if h.variant else description
                for name, fin in ((n, f) for c in names for n in codes_in(c.text)
                                  for f in h.finishes or [line.finish]):
                    out.append((PriceRow(
                        manufacturer=book.manufacturer, model=name,
                        kind="option_add" if option else "base",
                        list_price=amount(price.text), price_text=price.text,
                        pdf_page=pdf_page, printed_page=printed,
                        finish=_finish(fin), size=h.size or line.size,
                        description=text), price))

    # One model, finish and size at two prices on one page is a misread table.
    def same(r: PriceRow) -> tuple:
        return r.key, r.kind, r.finish, r.size, r.description

    seen: dict[tuple, set[float]] = {}
    for r, _ in out:
        seen.setdefault(same(r), set()).add(r.list_price)
    used: set[int] = set()
    for r, price in out:
        if len(seen[same(r)]) > 1:
            book.rejected.append(f"{where}: {r.model} {r.finish} {r.size} is printed at "
                                 f"more than one price on this page ({r.price_text})")
        else:
            book.rows.append(r)
            used.add(id(price))
    return len(used)


def _finish(text: str) -> str:
    """'US10A (US11)' -> 'US10A'."""
    return text.split("(")[0].strip()
