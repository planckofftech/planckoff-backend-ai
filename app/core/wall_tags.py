"""Which wall type a door sits in, read off the drawing's own tags.

The wall is never identified. That is the point.

An earlier attempt found the wall geometrically and then looked for its tag,
and it failed on four sets of seven: walls are hatched on one, drawn at 1/16"
on another, filled rather than outlined on a third. Measured across seven real
projects it reached 29%.

This works the other way round, the way an estimator does it:

    read the legend        what symbols does this set use for wall types?
    find those symbols     on the plan, drawn inside a circle or hexagon
    attach to the door     the nearest tag, when one is clearly nearest

No wall geometry anywhere in that chain, so none of the three drawing
conventions that defeated the geometric approach matter here.

Two doors are left open on purpose. Where two tags sit at much the same
distance the drawing is genuinely ambiguous, so both are returned and a person
picks -- being unsure and saying so costs nothing, while being confident and
wrong is what makes an estimator stop trusting the takeoff. And where the
answer is simply wrong, `corrections` already handles it: wall type is a field
of the door, keyed on the door number, so a fix survives every re-read.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field

from app.core.pdf_doc import PdfDoc

log = logging.getLogger(__name__)

# What a wall legend calls itself. Firms vary and all of these are real:
# "WALL TYPES" (BMK A5.14), "PARTITION TYPES" (AT&T A7.02), "ASSEMBLY TYPES"
# (CCS A-006), "PARTITION KEY", "WALL PARTITION KEY".
_LEGEND_CAPTION = re.compile(
    r"(WALL|PARTITION)\s*(TYPE|KEY|LEGEND|SCHEDULE|ASSEMBL)", re.I)
# A symbol is a very short token: 1, 5, A, B, P1, 2A, A3.1. The trailing
# decimal matters -- AT&T names half its partitions A3.1, B2.1 and so on, and a
# pattern without it read only part of that set's vocabulary.
_SYMBOL = re.compile(r"^[0-9]{1,2}[A-Z]?(\.[0-9])?$|^[A-Z][0-9]{0,2}(\.[0-9])?$")
# A legend row written as one line: "A1 _ 1" STUD WALL WITH 5/8" GYP EACH SIDE".
#
# Reading the row this way rather than pairing a symbol with whatever sits to
# its right is both simpler and far more reliable. Geometric pairing put F6's
# description against symbol 2 on AT&T, because two legend columns printed side
# by side look like one line.
_ROW = re.compile(r"^([0-9]{1,2}[A-Z]?(?:\.[0-9])?|[A-Z][0-9]{0,2}(?:\.[0-9])?)"
                  r"\s*[_\-–:]\s*(.{10,})$")
# What a wall is made of. A legend row states its build-up, and that is what
# separates a wall type from every other short token printed on the sheet.
#
# Without this the vocabulary came back as 48 symbols for a set with five wall
# types -- every letter and number on the page -- and Ellis confidently
# assigned detail numbers 17, 18 and 19 to its doors.
_BUILD_UP = re.compile(
    r"STUD|GYP|GWB|CMU|MASONRY|CONCRETE|BOARD|BD\.|FURRING|SHEATHING|"
    r"PARTITION|INSULATION|BATT|PLYWOOD|DRYWALL|PANEL", re.I)
# And a size. A wall row states what it is built of *and* how big those parts
# are: 3-5/8" studs, 5/8" board, 8" CMU. Prose that merely mentions gypsum does
# not, which is what let single letters like A, G and X into BMK's vocabulary
# when materials alone were the test.
_SIZED = re.compile(r"\d\s*[-/]?\s*\d*\s*/?\s*\d*\s*\"|\d+\s*(GA|GAUGE|MIL)\b",
                    re.I)
# How far around a tag to look for the shape enclosing it, in glyph heights.
_ENCLOSURE = 1.6
# How far from the glyph's centre a segment has to reach to count as part of
# the enclosure rather than part of the glyph itself.
_OFF_GLYPH = 0.45
# How much further the second-nearest tag must be before the nearest is taken
# as the answer without asking. Below this the two are comparable and the
# drawing is not telling us which -- so the caller is given both.
_DECISIVE = 1.6
# How far a tag may be from a door and still be a candidate, in door-widths.
# Generous: on sparsely tagged sheets the governing tag sits at the start of
# the wall run, and BMK's A3.10 averages seven door-widths.
_REACH = 10.0
# Most candidates ever returned for one door. Past three, a picker stops being
# a decision and becomes a research task.
_MAX_CANDIDATES = 3
# Angle bins for reading an enclosure's edges, in degrees.
_ANGLE_BIN = 15
# How many tags must share a shape before it is this sheet's
# convention rather than a coincidence.
_MIN_SHAPE_EXAMPLES = 3


@dataclass(slots=True)
class WallType:
    """One row of the legend: the symbol, and what it says."""

    symbol: str
    description: str = ""

    @property
    def rating(self) -> str:
        """The fire rating stated in the description, if it states one."""
        found = re.search(r"(\d+)\s*[-\s]?\s*(HR|HOUR)", self.description, re.I)
        return f"{found.group(1)} HR" if found else ""


@dataclass(slots=True)
class TagSighting:
    """A wall-type tag found on a plan sheet."""

    symbol: str
    x: float
    y: float
    page: int


@dataclass(slots=True)
class WallChoice:
    """What the drawing says about one door's wall."""

    symbol: str = ""
    candidates: list[str] = field(default_factory=list)
    distance: float = 0.0          # to the chosen tag, in door-widths
    decided: bool = False

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1 and not self.decided


def legend_symbols(doc: PdfDoc,
                   pages: list[tuple[int, str]]) -> list[WallType]:
    """The wall types this set defines, read from its legend sheet.

    Read for the vocabulary rather than the specification. Knowing that this
    set uses exactly 1-5 is what makes the tags findable at all: without it
    every number on a plan is a candidate, and a floor plan carries three
    hundred of them.

    The descriptions come along because they are free -- and because a
    description saying "ONE HOUR RATED" is worth surfacing beside a door whose
    schedule does not call it rated.
    """
    found: dict[str, WallType] = {}
    for page, title in pages:
        items = doc.text_items(page - 1)
        # The sheet's own title counts as a caption. King's City prints
        # "PARTITION TYPES" in its title block and nowhere in the body, and
        # requiring a caption in the text skipped a sheet carrying 49 rows.
        titled = bool(_LEGEND_CAPTION.search(title or ""))
        if not titled and not any(_LEGEND_CAPTION.search(i.text) for i in items):
            continue

        # First choice: rows written as one line, "A1 _ 3 5/8" STUD WALL...".
        for item in items:
            row = _ROW.match(item.text.strip())
            if not row:
                continue
            symbol, described = row.group(1), row.group(2).strip()
            if not (_BUILD_UP.search(described) and _SIZED.search(described)):
                continue
            if symbol not in found or len(described) > len(found[symbol].description):
                found[symbol] = WallType(symbol=symbol,
                                         description=described[:200])

        for item in items:
            text = item.text.strip()
            if not _SYMBOL.match(text):
                continue
            # The description is whatever is printed to the right of it, on
            # roughly the same line.
            line = [o for o in items
                    if abs((o.y0 + o.y1) / 2 - (item.y0 + item.y1) / 2) <= 6.0
                    and o.x0 > item.x1 and len(o.text.strip()) > 8]
            line.sort(key=lambda o: o.x0)
            described = " ".join(o.text.strip() for o in line[:3])
            # A symbol with nothing built beside it is not a wall type. This is
            # the whole filter: it is what keeps a detail callout, a keynote and
            # a grid letter out of the vocabulary.
            if not (_BUILD_UP.search(described) and _SIZED.search(described)):
                continue
            if text not in found or len(described) > len(found[text].description):
                found[text] = WallType(symbol=text, description=described[:200])
    types = sorted(found.values(), key=lambda w: w.symbol)
    log.info("wall_tags: %d wall type(s) defined: %s",
             len(types), ", ".join(t.symbol for t in types) or "none")
    return types


def _signature(doc: PdfDoc, page: int, x: float, y: float,
               size: float) -> frozenset[int]:
    """The shape drawn around a glyph, as the set of angles its edges run at.

    A diamond is four edges at 45 degrees and four at 135. A square is 0 and
    90. A hexagon is 0, 60 and 120. A circle, broken into short segments, is
    every angle there is. So the angles alone say which shape it is, without
    anyone having to name the shapes in advance.

    Segments close to the glyph's centre are ignored: those are the strokes of
    the character itself, not the ring around it.
    """
    reach = size * _ENCLOSURE
    bins: Counter[int] = Counter()
    for x0, y0, x1, y1 in doc.segments(page - 1, within=(x - reach, y - reach,
                                                         x + reach, y + reach)):
        dx, dy = x1 - x0, y1 - y0
        if math.hypot(dx, dy) < 0.5:
            continue
        near = max(math.hypot(x0 - x, y0 - y), math.hypot(x1 - x, y1 - y))
        if near <= size * _OFF_GLYPH:
            continue
        bins[round(math.degrees(math.atan2(dy, dx)) % 180 / _ANGLE_BIN)
             * _ANGLE_BIN] += 1
    return frozenset(angle for angle, count in bins.items() if count >= 2)


def tag_shape(doc: PdfDoc, page: int, vocabulary: set[str]) -> frozenset[int]:
    """What shape this sheet draws its wall tags in, learned from the sheet.

    The whole method turns on this. A plan is covered in short numbers -- door
    types, keynotes, revision marks, grid bubbles, dimension fragments -- and
    what separates a wall tag from all of them is not the character but the
    shape around it. Learning that shape rather than naming it is what lets one
    piece of code read a set that uses diamonds, a set that uses circles and a
    set that uses hexagons.

    Measured on CCS: partition tags came back as four edges at 45 degrees and
    four at 135 -- a diamond -- every time, while the toilet-accessory symbols
    that had contaminated the vocabulary sat in hexagons. The drawing was
    distinguishing them all along.
    """
    seen: Counter[frozenset[int]] = Counter()
    for item in doc.text_items(page - 1):
        if item.text.strip().upper() not in vocabulary:
            continue
        cx, cy = (item.x0 + item.x1) / 2, (item.y0 + item.y1) / 2
        size = max(item.x1 - item.x0, item.y1 - item.y0) or 4.0
        signature = _signature(doc, page, cx, cy, size)
        if signature:
            seen[signature] += 1
    if not seen:
        return frozenset()
    shape, count = seen.most_common(1)[0]
    # One example is not a convention. Below this the sheet has not shown us
    # enough to learn from, and every candidate is kept rather than filtered
    # against a shape that might be an accident.
    if count < _MIN_SHAPE_EXAMPLES:
        return frozenset()
    log.info("wall_tags: page %d tags a wall with edges at %s (%d of them)",
             page, sorted(shape), count)
    return shape


def tags_on(doc: PdfDoc, page: int, vocabulary: set[str],
            shape: frozenset[int] | None = None) -> list[TagSighting]:
    """Every wall-type tag drawn on this sheet.

    `shape` is what `tag_shape` learned. Where it is empty -- too few examples
    to be sure -- anything with a ring around it is kept, which is looser but
    still rejects bare text.
    """
    if shape is None:
        shape = tag_shape(doc, page, vocabulary)

    out: list[TagSighting] = []
    for item in doc.text_items(page - 1):
        text = item.text.strip().upper()
        if text not in vocabulary:
            continue
        cx, cy = (item.x0 + item.x1) / 2, (item.y0 + item.y1) / 2
        size = max(item.x1 - item.x0, item.y1 - item.y0) or 4.0
        signature = _signature(doc, page, cx, cy, size)
        if shape:
            # Most of the sheet's shape, not all of it. A tag carries extra
            # edges where a leader line or a wall passes behind it, and loses
            # one where an edge straddles a bin boundary or is partly hidden.
            #
            # Demanding every edge cost real answers: BMK went from 6% of doors
            # unmatched to 26%, and King's City to all of them, while the junk
            # this was meant to remove had already gone. Half the edges plus a
            # ring of some kind is enough to tell a tag from bare text.
            if len(shape & signature) * 2 < len(shape) or len(signature) < 2:
                continue
        elif len(signature) < 2:
            continue
        out.append(TagSighting(symbol=text, x=cx, y=cy, page=page))
    return out


def choose(tags: list[TagSighting], x: float, y: float,
           door_pt: float) -> WallChoice:
    """Which wall type this door sits in, or the shortlist if it is unclear.

    `(x, y)` is the door on the sheet -- its hinge, or the middle of its box.

    The nearest tag is taken when it is clearly nearest. When the next one is
    close behind, both are returned instead: two tags at the same distance is
    the drawing declining to answer, and guessing between them is how a takeoff
    earns a reputation for being confidently wrong.
    """
    if not tags:
        return WallChoice()

    ranked = sorted(
        ((math.hypot(t.x - x, t.y - y) / door_pt, t) for t in tags),
        key=lambda pair: pair[0])
    ranked = [(gap, tag) for gap, tag in ranked if gap <= _REACH]
    if not ranked:
        return WallChoice()

    nearest_gap, nearest = ranked[0]
    # Only tags with a different answer make it ambiguous. Three tags all
    # reading "3" agree, however far apart they are.
    others = [tag.symbol for gap, tag in ranked[1:_MAX_CANDIDATES + 1]
              if tag.symbol != nearest.symbol and gap <= nearest_gap * _DECISIVE]
    if not others:
        return WallChoice(symbol=nearest.symbol, candidates=[nearest.symbol],
                          distance=nearest_gap, decided=True)

    shortlist = [nearest.symbol]
    for symbol in others:
        if symbol not in shortlist:
            shortlist.append(symbol)
    return WallChoice(symbol="", candidates=shortlist[:_MAX_CANDIDATES],
                      distance=nearest_gap, decided=False)


def legend_candidates(doc: PdfDoc,
                      pages: list[tuple[int, str]]) -> list[int]:
    """Sheets most likely to carry the wall legend, best first.

    Only consulted when the deterministic reader came back empty, and only the
    first is normally worth paying to look at. Ranked by how much sized
    build-up text a sheet carries -- a partition-types sheet is dense with
    "3-5/8\" METAL STUD" and "5/8\" GYP. BD.", whatever layout it uses to
    present them, so the text is a good signal even where the structure is not.

    A titled sheet wins over an untitled one at the same density: King's City
    prints PARTITION TYPES in its title block, and that is the strongest hint
    a set gives.
    """
    ranked: list[tuple[int, int, int]] = []
    for page, title in pages:
        titled = 1 if _LEGEND_CAPTION.search(title or "") else 0
        sized = sum(
            1 for item in doc.text_items(page - 1)
            if _BUILD_UP.search(item.text) and _SIZED.search(item.text))
        if titled or sized >= 3:
            ranked.append((titled, sized, page))
    ranked.sort(reverse=True)
    return [page for _titled, _sized, page in ranked]
