"""How many units of a rate a schedule item is: square inches of a plate, feet of a seal.

Some books print rates, not prices: Hager's door plates by the hundred square
inches ("multiply the square inch size by the list price"), its thresholds and
seals by the foot ("U/M is feet"). The AI reads the rate; the count it is
multiplied by is worked out here, from the size the schedule writes, so a wrong
price cannot come from arithmetic two models happened to get the same wrong way.

    190S 10" x 34"             10 x 34 = 340 sq in -> 3.40
    190S x B4E x 10 x 34 in    the same plate, with "in" for the inch mark
    190S 10" x 1" LDW - 23"    10 high, 23 wide, priced as 10 x 24 -> 2.40   (LDW: less door width)
    190S 10" x 2" LDW          on a 36" door: 10 x 34 -> 3.40; no door width: None
    8400 4" X 1" LOW           LOW is LDW typed with an O: 4 x 35, priced as 4 x 36 -> 1.44
    4" x 1"                    a sliver, not a plate: None, for a person
    726 x LAR - 20'            20 feet
    488S @ HEAD [19 LF]        19 feet
    425 x 36 in                3 feet
    160SA x 36 x 84 in         an opening: head and two jambs, 3 + 7 + 7 = 17 feet
    413S x LAR                 on a 36" door: 3 feet (LAR: length as required)

A plate side is rounded up to the next even inch. The books' hundred square
inch charts step 2" at a time, and each says to price an odd size as the next
one up: Trimco "all odd sizes rounded up to the nearest even inch on both
height and width", Hager "for odd or fractional sizes use the next higher price
(8 x 31-1/2, price as 8 x 32)", Ives "6-1/2 x 33-1/2, use 8 x 34". A length that
is not a whole number of feet is not rounded: the book does not say which way,
so it is left for a person (None).
"""

from __future__ import annotations

import math
import re

from app.price_book import sizes

_INCH = r"(\d+(?:\.\d+)?(?:\s*-?\s*\d+/\d+)?)\s*\""
# Schedules write the inch mark as ", in, inch, ″ or ”. All become ".
_INCH_MARK = re.compile(r"(\d)\s*(?:[”″]|\bin\b\.?|\binch(?:es)?\b)", re.I)
# A plate is two sides, the second marked in inches. The first may go unmarked
# ("10 x 34 in"); it is a number of at most two digits standing on its own, so
# a model number ("8400 x ...") is never read as a side.
_SIDE = r"(\d{1,2}(?:\.\d+)?(?:\s*-?\s*\d+/\d+)?)"
_PLATE = re.compile(r"(?<![\w./-])" + _SIDE + r"\s*\"?\s*[xX×]\s*" + _SIDE + r"\s*\"")
# A single length in inches after an "x" or a dash ("425 x 36\"") that is not
# the first side of a plate.
_X_INCH = re.compile(r"(?:[xX×]|-)\s*" + _INCH + r"(?!\s*[xX×])")
# "Less door width", as schedules write it: LDW, LOW (the same, typed with an
# O), L.D.W., "less door width", "less width".
_LDW = re.compile(r"\bL\.?\s*[DO]\.?\s*W\b\.?|\bLESS\s+(DOOR\s+)?WIDTH\b", re.I)
# A plate narrower than this, written as its own width, is not a plate: it is
# a "less door width" the code did not recognise, and goes to a person.
_MIN_PLATE_WIDTH = 6.0
_DASH_INCH = re.compile(r"-\s*" + _INCH + r"(?!\s*[xX×])")
_FEET_INCH = re.compile(r"(\d+)\s*'\s*(?:-?\s*(\d+(?:\.\d+)?)\s*\")?")
_LF = re.compile(r"(\d+(?:\.\d+)?)\s*(?:LF|FT|FEET|FOOT)(?![A-Z])", re.I)
_BRACKET = re.compile(r"\[([^\]]*)\]")

# A unit is read by what it says, not matched against a list: books and models
# write the same unit many ways ("foot", "per LF", "lin. ft", "100 sq. in.").
# Anything that is neither a length nor an area -- each, pair, a pack of 100, a
# box, a roll -- is a price for what the book sells, counted by the schedule.
_AREA_UNIT = re.compile(r"\bsq\b|\bsquare\b", re.I)
_HUNDRED_SQ_IN = re.compile(r"(100|hundred)\s*(sq\.?|square)\s*(in\b|inch)", re.I)
_LENGTH_UNIT = re.compile(r"\b(foot|feet|ft|lf|lin|linear|lineal)\b", re.I)


def is_area(per: str) -> bool:
    """A rate by area: per 100 sq in, per sq ft."""
    return bool(_AREA_UNIT.search(per))


def is_length(per: str) -> bool:
    """A rate by length: per foot, per LF, lin. ft. A square foot is an area."""
    return bool(_LENGTH_UNIT.search(per)) and not is_area(per)


def _inches(text: str) -> float | None:
    got = sizes.dimensions(text + '" x 1"')
    return got[0] if got else None


def _marked(text: str) -> str:
    """The text with every inch mark written as \"."""
    return _INCH_MARK.sub(r'\1"', text)


def _even_up(inches: float) -> int:
    """The next even inch at or above a side: 34 -> 34, 35 -> 36, 31.5 -> 32."""
    return 2 * math.ceil(inches / 2 - 1e-9)


def plate_units(text: str, door_width: float | None = None) -> float | None:
    """Hundreds of square inches of the plate the text describes, or None."""
    text = _marked(text)
    for part in [b for b in _BRACKET.findall(text)] + [text]:
        m = _PLATE.search(part)
        if not m:
            continue
        height, second = _inches(m.group(1)), _inches(m.group(2))
        if height is None or second is None:
            return None
        # A bracketed size ("[10\" x 34\"]") is the plate as cut: no LDW in it.
        if part is text and _LDW.search(text):
            # "10 x 2 LDW": 10 high, the door's width less 2. The width is
            # written after a dash ("- 23\"") or comes from the door.
            dash = _DASH_INCH.search(text[m.end():])
            width = _inches(dash.group(1)) if dash else (door_width - second if door_width else None)
        else:
            width = second
            # "4\" x 1\"" for a mop plate is never a plate 1" wide: counting it
            # would price a sliver (TEST-38, $6.98 for a $244 plate).
            if min(height, width) < _MIN_PLATE_WIDTH and max(height, width) < _MIN_PLATE_WIDTH * 2:
                return None
        if width is None or width <= 0:
            return None
        return round(_even_up(height) * _even_up(width) / 100, 2)
    return None


_LAR = re.compile(r"\bLAR\b", re.I)
# The shortest opening height read as a door: below it, "36 x 5" is not an opening.
_DOOR_HEIGHT = 60.0


def length_units(text: str, door_width: float | None = None) -> float | None:
    """Feet of length the text gives, when it is a whole number of feet.

    "LAR" with no length written is cut to the opening: the door's width.
    """
    feet = None
    text = _marked(text)
    for part in _BRACKET.findall(text) + [text]:
        m = _LF.search(part)
        if m:
            feet = float(m.group(1))
            break
        m = _FEET_INCH.search(part)
        if m:
            feet = int(m.group(1)) + (float(m.group(2)) / 12 if m.group(2) else 0)
            break
        m = re.search(_INCH, part)
        if m and part is not text:
            inches = _inches(m.group(1))
            feet = inches / 12 if inches else None
            break
        if part is not text:
            continue
        m = _PLATE.search(part)
        if m:
            # "160SA x 36 x 84 in": an opening, width by height. A seal round it
            # runs the head and both jambs, as NGP sizes its own door sets by the
            # opening (160V 3'X7'): 3 + 7 + 7 = 17 feet. A second side shorter
            # than a door is not an opening: left for a person.
            width, height = _inches(m.group(1)), _inches(m.group(2))
            if width and height and height >= _DOOR_HEIGHT:
                feet = (width + 2 * height) / 12
                break
            continue
        # "425 x 36 in": one length in inches.
        m = _X_INCH.search(part)
        if m:
            inches = _inches(m.group(1))
            feet = inches / 12 if inches else None
            break
    if feet is None and door_width and _LAR.search(text):
        feet = door_width / 12
    if feet is None or abs(feet - round(feet)) > 1e-6:
        return None
    return float(round(feet))


def expected_units(per: str, text: str, door_width: float | None = None) -> tuple[bool, float | None]:
    """(is this a rate code counts, the count). Not a rate: (False, None)."""
    if is_area(per):
        # The plate count is in hundreds of square inches, the unit Hager and
        # Rockwood print. Any other area (per sq in, per sq ft) is still a rate,
        # but one we do not count: it goes to a person, never through as one.
        return True, plate_units(text, door_width) if _HUNDRED_SQ_IN.search(per) else None
    if is_length(per):
        return True, length_units(text, door_width)
    return False, None


def same_size_row(row: str, text: str, door_width: float | None = None) -> bool:
    """Whether a price row read is a size the book prints ready-priced, and it
    is the plate's size from the schedule.

    Hager prints a few common plates as one price under the rate table
    ("10" x 34" (List Pricing) 60.43"): that price is the price of one, not a
    rate, but only for a plate of exactly that size.
    """
    row = row.replace("”", '"').replace("″", '"')
    if not _PLATE.search(row):
        return False
    wanted = plate_units(text, door_width)
    return wanted is not None and plate_units(row) == wanted


def door_width(text: str | float | None) -> float | None:
    """A door width as a schedule or the frontend writes it, in inches: 36, 36", 3'-0"."""
    if text is None or text == "":
        return None
    if isinstance(text, (int, float)):
        return float(text) if text > 0 else None
    t = _marked(str(text).strip())
    m = _FEET_INCH.fullmatch(t.replace(" ", "")) or _FEET_INCH.match(t)
    if m:
        return int(m.group(1)) * 12 + (float(m.group(2)) if m.group(2) else 0)
    try:
        return float(t.rstrip('"').strip()) or None
    except ValueError:
        return _inches(t.rstrip('"'))
