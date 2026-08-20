"""Read the sizes a door schedule prints, and turn them into relative widths.

A schedule states a door as feet and inches -- `3' - 0"`, `6' - 0"`,
`(2)3' - 0"` for a pair. Nothing else in this codebase has needed to understand
that until now: a box drawn around a detected door should be the size of the
door, and the drawing is the only thing that knows.

Deliberately relative, not absolute. Turning feet into points needs the sheet's
drawing scale, which is printed as text (`1/8" = 1'-0"`) that is not always
parseable and is sometimes wrong. Ratios need none of that: if the median door
on a sheet is 3'-0" and renders about 36 pt wide, then a 6'-0" pair renders
about 72 pt. That is enough to make a double door look like a double door,
which is the whole point of drawing the box at all.
"""

from __future__ import annotations

import re

# `3' - 0"`, `3'-0"`, `3' 0"`, `3'`, and the `(2)` prefix a pair is written with.
_FEET_INCHES = re.compile(
    r"""(?:\((?P<leaves>\d)\)\s*)?      # (2) for a pair
        (?P<feet>\d+)\s*['’]            # feet, then a foot mark
        (?:\s*-?\s*
           (?P<inches>\d+)              # whole inches
           (?:\s+(?P<num>\d+)/(?P<den>\d+))?   # and a fraction
           \s*["”]?
        )?""",
    re.VERBOSE,
)
# A leaf wider than this is not a door leaf; a schedule cell holding it is
# something else -- an opening width, a wall length, a typo.
_MAX_SENSIBLE_FT = 20.0


def parse_feet(text: str) -> float | None:
    """Feet as a number, or None when the text is not a size.

    A `(2)` prefix means a pair, and the number after it is the width of *one*
    leaf -- so the opening is twice that. Reading it as a single leaf makes a
    double door look like a single one, which is exactly the distinction this
    exists to preserve.
    """
    if not text:
        return None
    match = _FEET_INCHES.search(str(text))
    if not match:
        return None

    feet = float(match.group("feet"))
    if match.group("inches"):
        feet += float(match.group("inches")) / 12.0
    if match.group("num") and match.group("den"):
        denominator = float(match.group("den"))
        if denominator:
            feet += float(match.group("num")) / denominator / 12.0

    leaves = match.group("leaves")
    if leaves:
        feet *= float(leaves)

    return feet if 0 < feet <= _MAX_SENSIBLE_FT else None


def median_width_ft(widths: list[str]) -> float | None:
    """The typical door width on this schedule, in feet."""
    values = [v for v in (parse_feet(w) for w in widths) if v is not None]
    if not values:
        return None
    values.sort()
    return values[len(values) // 2]


def door_span_pt(width_text: str, median_ft: float | None,
                 base_pt: float) -> float:
    """How wide to draw one door, in points, from what the schedule says.

    `base_pt` is what the *median* door measures on this sheet. Everything else
    is scaled off it, so a pair comes out twice the width of a single without
    anyone having to parse the drawing's scale.

    Falls back to `base_pt` whenever the width cannot be read -- an unreadable
    cell should give a normal-looking box, not a missing one.
    """
    feet = parse_feet(width_text)
    if feet is None or not median_ft:
        return base_pt
    return base_pt * (feet / median_ft)


def feet_inches(feet: float) -> str:
    """Format a measured size the way a drawing writes it.

    Rounded to the nearest inch. The measurement is good to a fraction of a
    point, but reporting `2' - 11.87"` would imply a precision the drawing
    itself does not claim -- doors come in whole inches.
    """
    if feet <= 0:
        return ""
    total = round(feet * 12)
    return f"{total // 12}' - {total % 12}\""
