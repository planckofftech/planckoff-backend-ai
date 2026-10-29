"""Door numbers printed in two pieces: the number, and its letter beneath it.

Some sets draw a door tag as a stacked mark -- `101` on top, `A` under it, both
inside one symbol -- and the PDF holds them as two separate pieces of text. A
schedule calls that door `101A`, so a search for `101A` finds nothing: on one
set every one of 34 doors was printed on its floor plan this way, and not one
was found. The plan pass then had no sheet with a door on it, skipped every
plan, and placed 1 door of 34.

So the two pieces are put back together before anything looks for a door
number. Only where the result is a number the schedule actually carries, so
nothing is invented: a room number with a letter under it that no door is
called stays exactly as it was.
"""

from __future__ import annotations

from app.core.pdf_doc import TextItem

# The letter sits about a line below the number: 1.15 of the number's height
# on the set this was measured on. Two lines down is another label.
_MAX_DROP = 1.7
# The two are set in one size; a letter at another size is a different label.
_SIZE_TOLERANCE = 0.25
# Centred on the number, give or take part of its width.
_CENTRE_TOLERANCE = 0.6


def _suffix(text: str) -> bool:
    return 1 <= len(text) <= 2 and text.isalpha() and text.isupper()


def join(items: list[TextItem], tags: set[str]) -> list[TextItem]:
    """These items, with each stacked number-and-letter that names a scheduled
    door replaced by one item reading as that door's number."""
    if not tags:
        return items
    letters = [i for i in items if _suffix(i.text.strip())]
    if not letters:
        return items
    used: set[int] = set()
    joined: list[TextItem] = []
    for number in items:
        text = number.text.strip()
        if not text.isdigit():
            continue
        height = max(number.size, number.y1 - number.y0, 1e-6)
        width = max(number.x1 - number.x0, height)
        best = None
        for letter in letters:
            if id(letter) in used:
                continue
            drop = letter.cy - number.cy
            if not 0 < drop <= height * _MAX_DROP:
                continue
            if abs(letter.cx - number.cx) > width * _CENTRE_TOLERANCE:
                continue
            if abs(letter.size - number.size) > number.size * _SIZE_TOLERANCE:
                continue
            if text + letter.text.strip() not in tags:
                continue
            if best is None or drop < best.cy - number.cy:
                best = letter
        if best is None:
            continue
        used.update((id(number), id(best)))
        joined.append(TextItem(
            x0=min(number.x0, best.x0), y0=number.y0,
            x1=max(number.x1, best.x1), y1=best.y1,
            text=text + best.text.strip(), size=number.size,
            horizontal=number.horizontal))
    if not joined:
        return items
    return [i for i in items if id(i) not in used] + joined
