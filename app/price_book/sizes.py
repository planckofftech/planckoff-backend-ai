"""Sizes as hardware schedules write them, made comparable.

Two kinds occur in the books read so far:

  device length  "3'", "4'" -- an exit device is priced by the door it fits
  dimensions     '4-1/2" x 4-1/2"' -- a hinge, height by width

A schedule writes the same hinge as "4-1/2 x 4-1/2", "4.5 x 4.5", "4 1/2x4 1/2"
or "4½ x 4½"; all of them become (4.5, 4.5). Nothing here rounds or converts a
door width to a device length -- 42" is not quietly made 4'.
"""

from __future__ import annotations

import re

# Whole feet, but not the feet of 3'-6" -- that door needs a person to choose.
_FEET = re.compile(r"(?<![\d/.-])(\d{1,2})\s*'(?!\s*-?\s*[1-9]\d?\s*\")")
_FEET_INCHES = re.compile(r"(?<![\d/.-])(\d{1,2})'\s*-?\s*0\s*\"")
_INCHES = re.compile(r"(?<![\d/.-])(\d{2})\s*\"(?!\s*x)")
_NUMBER = r"\d+(?:\.\d+)?(?:[\s-]+\d+/\d+)?|\d+/\d+"
_DIMS = re.compile(rf"({_NUMBER})\s*\"?\s*[xX×]\s*({_NUMBER})\s*\"?")
_SINGLE = re.compile(rf"^\s*({_NUMBER})\s*\"?\s*$")


def inches(text: str) -> float:
    """'4-1/2' -> 4.5, '4 1/2' -> 4.5, '4.5' -> 4.5, '3/4' -> 0.75."""
    text = text.replace("½", "-1/2").replace("¼", "-1/4").replace("¾", "-3/4").strip()
    total = 0.0
    for part in re.split(r"[\s-]+", text):
        if "/" in part:
            num, den = part.split("/")
            total += int(num) / int(den)
        elif part:
            total += float(part)
    return total


def dimensions(text: str) -> tuple[float, ...] | None:
    """The dimensions a size names, or None if it names none."""
    text = text.replace("½", "-1/2").replace("¼", "-1/4").replace("¾", "-3/4")
    text = text.replace("”", '"').replace("″", '"')
    m = _DIMS.search(text)
    if m:
        return (inches(m.group(1)), inches(m.group(2)))
    m = _SINGLE.match(text)
    if m:
        return (inches(m.group(1)),)
    return None


def device_length(text: str) -> str | None:
    """"4'", "4'-0\"" or "48\"" -> "4'"; None when no length is written.

    Only whole feet are taken. A 42" door is not a device length, and which
    device fits it is a choice for a person, not for this function.
    """
    text = text.replace("’", "'").replace("”", '"').replace("″", '"')
    m = _FEET_INCHES.search(text) or _FEET.search(text)
    if m:
        return f"{int(m.group(1))}'"
    m = _INCHES.search(text)
    if m and int(m.group(1)) % 12 == 0:
        return f"{int(m.group(1)) // 12}'"
    return None


def strip_sizes(text: str) -> str:
    """The text with any sizes taken out, so they are not read as a model."""
    text = _DIMS.sub(" ", text)
    text = _FEET_INCHES.sub(" ", text)
    return _FEET.sub(" ", text)
