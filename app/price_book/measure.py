"""How many units of a rate a schedule item is: square inches of a plate, feet of a seal.

Some books print rates, not prices: Hager's door plates by the hundred square
inches ("multiply the square inch size by the list price"), its thresholds and
seals by the foot ("U/M is feet"). The AI reads the rate; the count it is
multiplied by is worked out here, from the size the schedule writes, so a wrong
price cannot come from arithmetic two models happened to get the same wrong way.

    190S 10" x 34"             10 x 34 = 340 sq in -> 3.40
    190S 10" x 1" LDW - 23"    10 high, 23 wide    -> 2.30   (LDW: less door width)
    190S 10" x 2" LDW          on a 36" door: 10 x 34 -> 3.40; no door width: None
    726 x LAR - 20'            20 feet
    488S @ HEAD [19 LF]        19 feet
    413S x LAR                 on a 36" door: 3 feet (LAR: length as required)

A plate side is rounded up to the whole inch, as Hager says ("8 x 31-1/2, price
as 8 x 32"). A length that is not a whole number of feet is not rounded: the
book does not say which way, so it is left for a person (None).
"""

from __future__ import annotations

import math
import re

from app.price_book import sizes

_INCH = r"(\d+(?:\.\d+)?(?:\s*-?\s*\d+/\d+)?)\s*\""
_PLATE = re.compile(_INCH + r"\s*[xX×]\s*" + _INCH)
_LDW = re.compile(r"\bLDW\b", re.I)
_DASH_INCH = re.compile(r"-\s*" + _INCH + r"(?!\s*[xX×])")
_FEET_INCH = re.compile(r"(\d+)\s*'\s*(?:-?\s*(\d+(?:\.\d+)?)\s*\")?")
_LF = re.compile(r"(\d+(?:\.\d+)?)\s*(?:LF|FT|FEET|FOOT)(?![A-Z])", re.I)
_BRACKET = re.compile(r"\[([^\]]*)\]")

AREA_UNITS = {"100 sq in", "100 square inches", "hundred square inches", "per 100 sq in"}
LENGTH_UNITS = {"foot", "feet", "ft", "lf", "per foot", "linear foot", "lineal foot"}


def _inches(text: str) -> float | None:
    got = sizes.dimensions(text + '" x 1"')
    return got[0] if got else None


def plate_units(text: str, door_width: float | None = None) -> float | None:
    """Hundreds of square inches of the plate the text describes, or None."""
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
        if width is None or width <= 0:
            return None
        return round(math.ceil(height) * math.ceil(width) / 100, 2)
    return None


_LAR = re.compile(r"\bLAR\b", re.I)


def length_units(text: str, door_width: float | None = None) -> float | None:
    """Feet of length the text gives, when it is a whole number of feet.

    "LAR" with no length written is cut to the opening: the door's width.
    """
    feet = None
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
    if feet is None and door_width and _LAR.search(text):
        feet = door_width / 12
    if feet is None or abs(feet - round(feet)) > 1e-6:
        return None
    return float(round(feet))


def expected_units(per: str, text: str, door_width: float | None = None) -> tuple[bool, float | None]:
    """(is this a rate code counts, the count). Not a rate: (False, None)."""
    unit = per.strip().lower()
    if unit in AREA_UNITS or ("sq" in unit and "in" in unit):
        return True, plate_units(text, door_width)
    if unit in LENGTH_UNITS:
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
    t = str(text).strip()
    m = _FEET_INCH.fullmatch(t.replace(" ", "")) or _FEET_INCH.match(t)
    if m:
        return int(m.group(1)) * 12 + (float(m.group(2)) if m.group(2) else 0)
    try:
        return float(t.rstrip('"').strip()) or None
    except ValueError:
        return _inches(t.rstrip('"'))
