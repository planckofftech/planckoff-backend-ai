"""Decide which schedule door each detected shape is, once, for a whole sheet.

The obvious way to do this is per door: for each shape, take the nearest tag.
That was the way it was done, and it is wrong for a reason worth writing down.

Door numbers on a real plan sit very close together. On one sheet tags 117 and
118 are 37 pt apart and 127 and 128 are 36 pt, while a detection's own position
is good to perhaps half a door. So "nearest" is a coin toss between neighbours,
and nothing stopped two shapes from both landing on tag 117. The stage that
tidied up afterwards kept one of them and deleted the other -- so a wrong guess
did not merely mislabel one door, it made a real door disappear. Three doors
vanished that way on a 37-door sheet.

The fix is not a better distance threshold. It is to stop deciding one door at
a time. A sheet offers a set of shapes and a set of numbers, each number belongs
to exactly one door, and the assignment has to be made as one decision over all
of them. Then a shape losing tag 117 to a closer rival takes 118 instead of
being thrown away.

Evidence is used strongest first:

    the model's own reading   it can see "119" printed beside the door, and
                              that beats any amount of arithmetic about
                              distance. This was already being parsed and then
                              thrown away.
    distance                  everything left over, exclusively, nearest pair
                              first.

Distances are in points, on both axes. The previous version compared
`hypot(dx/width, dy/height)` against a window expressed in widths, which on a
3024x2160 pt sheet meant a window 85 pt wide and 61 pt tall -- an ellipse
nobody chose and nobody could see.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

log = logging.getLogger(__name__)

# How far from a door its number may be printed. Tags sit beside doors, never
# on them, and the detection has its own error on top -- measured from the tag
# to the nearest edge of the door box, not to its centre, so a wide pair is not
# penalised for being wide.
TAG_WINDOW_PT = 85.0
# What the number the model *read* is worth, expressed as points of distance.
#
# It was worth a great deal more, and that was wrong. Allowing a read number to
# override position anywhere within 200 pt -- about seven door widths on a real
# sheet -- let a misread put a box on one door under its neighbour's number, on
# a sheet where the boxes themselves were in the right places. Position, once
# it is settled one-to-one across the whole sheet, measured 27 of 27 correct on
# real drawing geometry; the reading has no comparable evidence behind it.
#
# So it is demoted to what it is good for: breaking a tie. Where two numbers
# are the same distance from a door, position has no answer at all and the
# model looking straight at the printed digits is better than a coin toss.
# Where position has an answer, position wins.
READ_BONUS_PT = 2.0


@dataclass(slots=True)
class Spot:
    """A thing to be matched, in page points. Box for a door, point for a tag."""

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


def gap_pt(box: Spot, tag: Spot) -> float:
    """Points from a tag's centre to the nearest edge of a door's box.

    Centre to centre would punish a double door for being twice as wide, which
    is exactly backwards: its own tag is printed at one end of it.
    """
    dx = max(box.x0 - tag.cx, 0.0, tag.cx - box.x1)
    dy = max(box.y0 - tag.cy, 0.0, tag.cy - box.y1)
    return math.hypot(dx, dy)


def assign(doors: list[Spot], read: list[str], tags: dict[str, Spot],
           *, window_pt: float = TAG_WINDOW_PT) -> dict[int, str]:
    """Which schedule door each shape is. One tag per door, one door per tag.

    `doors` are the final boxes -- measured off the drawing where there was an
    arc to measure -- and `read` is what the model said each one's number was,
    "" where it read none. `tags` is where each scheduled number is actually
    printed on this sheet.

    Returns {door index: tag}. A door missing from the result carries no number
    that can be accounted for -- and that has to mean exactly that, not "two
    shapes wanted one number and this one lost".

    A number is given only where the shape and the number are each other's
    closest: this box's nearest number is that one, AND that number's nearest
    box is this one. Anything short of mutual gets nothing.

    That symmetry is the whole point, and it was learned the expensive way.
    Handing out numbers nearest-pair-first, with every box obliged to take
    something, made a box sitting 3 pt from number 119 come back labelled 118 --
    because another box had already taken 119, and 118 was the next one going
    spare, 53 pt away on a different door. Three more went the same way on one
    sheet. Requiring agreement makes that box unnumbered instead, which is
    honest: an estimator can check a blank, but a confident wrong number looks
    exactly like a right one.
    """
    # What the model read is folded in as a small discount on distance, so it
    # can settle a tie and nothing more -- see READ_BONUS_PT.
    def cost(index: int, door: Spot, tag: str, spot: Spot) -> float:
        said = (read[index] if index < len(read) else "").strip()
        return gap_pt(door, spot) - (READ_BONUS_PT if tag == said else 0.0)

    near: dict[int, tuple[float, str]] = {}
    for index, door in enumerate(doors):
        best = min(((cost(index, door, tag, spot), tag)
                    for tag, spot in tags.items()
                    if gap_pt(door, spot) <= window_pt), default=None)
        if best is not None:
            near[index] = best

    claimed: dict[str, tuple[float, int]] = {}
    for tag, spot in tags.items():
        best = min(((cost(index, door, tag, spot), index)
                    for index, door in enumerate(doors)
                    if gap_pt(door, spot) <= window_pt), default=None)
        if best is not None:
            claimed[tag] = best

    out = {index: tag for index, (_c, tag) in near.items()
           if claimed.get(tag, (0.0, -1))[1] == index}

    log.info("door_match: %d doors, %d tags -> %d agreed on both sides, "
             "%d doors left with no number", len(doors), len(tags), len(out),
             len(doors) - len(out))
    return out
