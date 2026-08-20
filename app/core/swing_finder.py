"""Find the actual door beside a point, and measure it.

The vision model is good at *"a door is around here"* -- 94% of them on a real
sheet -- and bad at saying exactly where. Drawing a box from that point plus an
assumed size gives a fabricated rectangle: it clips half the swing, or lands on
nothing at all, and no amount of adjusting the assumed size fixes either.

The drawing already contains the answer. A door in plan is a quarter-circle arc
struck from the hinge, and on these sheets it is drawn as a chain of very short
straight segments. Fit a circle to that chain and everything falls out at once:

    centre      the hinge
    radius      the leaf width -- the door's real size, measured
    angles      which way it opens
    extent      an exact box, not a guessed one
    absence     no arc means no door, which is how a false positive is caught

Searching a whole sheet for arcs does not work -- 45,000 segments, and the arc,
the leaf and the wall all touch. That was tried and abandoned. This works
because it never searches a sheet: the detector has already narrowed it to a
window a couple of door-widths across, and inside that window the problem is
small.

So neither half stands alone. The model cannot measure; the geometry cannot
search. Together they do the job.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass

from app.core.pdf_doc import PdfDoc

log = logging.getLogger(__name__)

# How far around the given point to look, as a multiple of the expected door
# width. Two door-widths covers the detector's error and the swing itself.
_WINDOW = 2.2
# A chain shorter than this is a tick, a hatch or a corner, not an arc.
_MIN_CHAIN = 4
# How far a point may sit off the fitted circle, in points.
#
# Measured, not chosen: on one sheet the door arcs fit to 0.036 and the curves
# that impersonate them -- basin bowls, chair backs, corner fillets -- fit to
# 0.169. Sweeping the bar from 0.6 down, the confirmed set went from 49 arcs at
# four different radii to 43 arcs all at exactly the sheet's door radius. That
# is where a loose threshold stops flattering the result.
_MAX_RESIDUAL = 0.08
# A door swing is a quarter turn. The band is wide enough for a chain that is
# clipped by the window, and narrow enough to exclude the full circles that
# keynote bubbles and grid markers are drawn as.
_MIN_SPAN_DEG = 45.0
_MAX_SPAN_DEG = 135.0
# A candidate radius must be within this fraction of the sheet's own typical
# door radius. Measured, never assumed -- see `calibrate`. Kept wide enough for
# the wider doors a schedule really carries: a set with 3', 6', 9' and 12'
# openings draws leaves at several radii, and a tight band would reject every
# one that is not the commonest.
_RADIUS_TOLERANCE = 0.45
# ...but a fit this much worse than the best one in the same window is not the
# door, whatever its radius. Doors fit to hundredths of a point.
_RESIDUAL_RATIO = 3.0
# Endpoints closer than this are the same vertex.
_JOIN_TOL = 0.05
# How far to look for a door panel, and how long one may be, both in door
# widths. The upper bound is what keeps a wall out: a wall runs on past the
# opening, a leaf stops at it.
_LEAF_WINDOW = 1.2
_LEAF_MIN = 0.55
_LEAF_MAX = 1.35
# How far a pair's hinge separation may sit from a clean two leaf-lengths,
# as a fraction of one leaf. Wide enough for the slop in a real drawing,
# narrow enough that the next door along -- typically one leaf-length off --
# cannot be mistaken for the other half of this one.
_PAIR_TOLERANCE = 0.30
# The partner hinges two leaves off and sweeps a leaf beyond that, so its
# window has to be wider than a single door's.
_PARTNER_WINDOW = 2.0
# Following a door number's leader line, all in leaf-lengths: how far to look,
# how near a line must start to count as leaving the number, and how long it
# must be to be a leader rather than part of the box drawn round the number.
_LEADER_WINDOW = 6.0
_LEADER_START = 1.0
_LEADER_MIN = 0.6
# How far a door number may be from its own arc, in leaf-lengths. Deliberately
# generous: what stops a door taking its neighbour's swing is that the
# neighbour is nearer to it, not this bound. It only stops the search running
# off across the sheet.
_TAG_SWEEP = 6.0
# How many doors must agree before a sheet's habit is a habit, and how far
# outside it a door may sit before it is an outlier rather than a door.
_MIN_HABIT = 4
_GAP_OUTLIER = 3.0
# Two leaves of one pair are the same size. A neighbour need not be.
_LEAF_MATCH = 0.20
# Two fitted hinges this close, in points, are the same arc found twice. Well
# under the ~36 pt gap between neighbouring doors on a real plan.
_HINGE_TOL = 3.0


@dataclass(slots=True)
class Swing:
    """One door swing, measured off the drawing."""

    hinge_x: float
    hinge_y: float
    radius: float          # points; the door leaf's length
    start_deg: float
    end_deg: float
    residual: float
    x0: float              # exact bounding box, in page points
    y0: float
    x1: float
    y1: float

    @property
    def span_deg(self) -> float:
        return self.end_deg - self.start_deg


def _fit_circle(points: list[tuple[float, float]]
                ) -> tuple[float, float, float, float] | None:
    """Least-squares circle through these points (Kasa), or None if degenerate.

    Algebraic rather than iterative: it is closed-form, and on a chain that
    really is an arc it lands within hundredths of a point.
    """
    n = len(points)
    mean_x = sum(p[0] for p in points) / n
    mean_y = sum(p[1] for p in points) / n
    u = [(x - mean_x, y - mean_y) for x, y in points]

    suu = sum(a * a for a, _ in u)
    svv = sum(b * b for _, b in u)
    suv = sum(a * b for a, b in u)
    determinant = 2 * (suu * svv - suv * suv)
    if abs(determinant) < 1e-9:
        return None

    suuu = sum(a ** 3 for a, _ in u)
    svvv = sum(b ** 3 for _, b in u)
    suvv = sum(a * b * b for a, b in u)
    svuu = sum(b * a * a for a, b in u)
    centre_u = ((suuu + suvv) * svv - (svvv + svuu) * suv) / determinant
    centre_v = ((svvv + svuu) * suu - (suuu + suvv) * suv) / determinant

    cx, cy = centre_u + mean_x, centre_v + mean_y
    radii = [math.hypot(x - cx, y - cy) for x, y in points]
    radius = sum(radii) / len(radii)
    residual = math.sqrt(sum((r - radius) ** 2 for r in radii) / len(radii))
    return cx, cy, radius, residual


def _chains(segments: list[tuple[float, float, float, float]]
            ) -> list[list[tuple[float, float]]]:
    """Group segments that touch end to end, and return each group's points."""
    key = lambda p: (round(p[0] / _JOIN_TOL), round(p[1] / _JOIN_TOL))
    touching: dict[tuple[int, int], list[int]] = defaultdict(list)
    ends = [((s[0], s[1]), (s[2], s[3])) for s in segments]
    for index, (a, b) in enumerate(ends):
        touching[key(a)].append(index)
        touching[key(b)].append(index)

    seen: set[int] = set()
    out: list[list[tuple[float, float]]] = []
    for start in range(len(ends)):
        if start in seen:
            continue
        stack, group = [start], []
        while stack:
            index = stack.pop()
            if index in seen:
                continue
            seen.add(index)
            group.append(index)
            for end in ends[index]:
                for other in touching[key(end)]:
                    if other not in seen:
                        stack.append(other)
        if len(group) >= _MIN_CHAIN:
            out.append(sorted({p for i in group for p in ends[i]}))
    return out


def _arc_from(points: list[tuple[float, float]], expected_r: float | None
              ) -> Swing | None:
    """A swing, if these points are an arc of about the right size."""
    if len(points) < _MIN_CHAIN:
        return None
    fit = _fit_circle(points)
    if fit is None:
        return None
    cx, cy, radius, residual = fit
    if residual > _MAX_RESIDUAL or radius <= 0:
        return None
    if expected_r and abs(radius - expected_r) > expected_r * _RADIUS_TOLERANCE:
        return None

    angles = sorted(math.degrees(math.atan2(y - cy, x - cx)) for x, y in points)
    span = angles[-1] - angles[0]
    if not (_MIN_SPAN_DEG <= span <= _MAX_SPAN_DEG):
        return None

    # The door occupies the arc *and* the leaf running out to it from the hinge,
    # so the hinge is part of the shape and the box has to include it.
    xs = [p[0] for p in points] + [cx]
    ys = [p[1] for p in points] + [cy]
    return Swing(cx, cy, radius, angles[0], angles[-1], residual,
                 min(xs), min(ys), max(xs), max(ys))


def arcs_near(doc: PdfDoc, page: int, x: float, y: float,
              expected_r: float | None = None,
              door_pt: float = 36.0) -> list[Swing]:
    """Every arc in the window around (x, y), unranked."""
    reach = max(door_pt, expected_r or 0) * _WINDOW
    window = (x - reach, y - reach, x + reach, y + reach)
    segments = doc.segments(page - 1, within=window)
    if not segments:
        return []
    return [arc for arc in (_arc_from(points, expected_r)
                            for points in _chains(segments))
            if arc is not None]


@dataclass(slots=True)
class Leaf:
    """A door panel found with no arc turning about it, in page points.

    A swinging door is not the only kind, and until now it was the only kind
    this module could see. A pocket door, a slider, a barn door and a roll-up
    shutter are all drawn as a panel with no arc at all -- so every one of them
    came back as "no swing found", which the pipeline reads as "nothing is
    there" and drops. On a warehouse whose schedule is two thirds sectional
    overhead doors, that is most of the building.

    Finding the panel says the opposite thing, and says it from the drawing: a
    door IS here, it simply does not swing.
    """

    x0: float
    y0: float
    x1: float
    y1: float
    length: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


def find_leaf(doc: PdfDoc, page: int, x: float, y: float,
              door_pt: float = 36.0) -> Leaf | None:
    """The door panel nearest (x, y), where no arc turns about it.

    A leaf is a single straight line about as long as the opening is wide. Two
    things have to be told apart from it and both are settled by length:

      a wall      runs on well past the opening. Anything much longer than a
                  door is the wall the door sits in, not the door.
      a tick      dimension arrows, hatching, door stops. Anything much shorter.

    So: straight, isolated, and about a door long. Returns None when there is
    no such line, which is the honest answer for a cased opening -- a gap in a
    wall with nothing drawn across it.
    """
    reach = door_pt * _LEAF_WINDOW
    window = (x - reach, y - reach, x + reach, y + reach)
    lo, hi = door_pt * _LEAF_MIN, door_pt * _LEAF_MAX

    best: Leaf | None = None
    best_gap = float("inf")
    for x0, y0, x1, y1 in doc.segments(page - 1, within=window):
        length = math.hypot(x1 - x0, y1 - y0)
        if not (lo <= length <= hi):
            continue
        gap = math.hypot((x0 + x1) / 2 - x, (y0 + y1) / 2 - y)
        if gap < best_gap:
            best_gap, best = gap, Leaf(x0, y0, x1, y1, length)

    if best is not None:
        log.debug("swing_finder: leaf %.0f pt long near (%.0f, %.0f) on page %s",
                  best.length, x, y, page)
    return best


def leader_end(doc: PdfDoc, page: int, x: float, y: float,
               door_pt: float = 36.0) -> tuple[float, float] | None:
    """Where the leader line from this door number points.

    Most sheets print a door's number at its door. Some print it clear of the
    plan and draw a line to the opening -- on one basement plan the nearest
    arc to any tag is two leaf-lengths away, because the tags are all out on
    leaders. Refusing to guess there was right, but it left seven scheduled
    doors with no position at all.

    The drawing says where they are: a leader starts at the number and ends at
    the thing it labels. Follow it and search from the far end instead.

    Returns None when no line leaves the tag, which is the ordinary case.
    """
    reach = door_pt * _LEADER_WINDOW
    near = door_pt * _LEADER_START
    shortest = door_pt * _LEADER_MIN

    best: tuple[float, float] | None = None
    longest = 0.0
    for x0, y0, x1, y1 in doc.segments(page - 1,
                                       within=(x - reach, y - reach,
                                               x + reach, y + reach)):
        for ax, ay, bx, by in ((x0, y0, x1, y1), (x1, y1, x0, y0)):
            if math.hypot(ax - x, ay - y) > near:
                continue
            length = math.hypot(bx - ax, by - ay)
            # The longest line leaving the tag is the leader; the short ones
            # are the box drawn round the number.
            if length >= shortest and length > longest:
                longest, best = length, (bx, by)

    if best:
        log.info("swing_finder: door number at (%.0f, %.0f) on page %s is on a "
                 "%.0f pt leader pointing at (%.0f, %.0f)", x, y, page,
                 longest, best[0], best[1])
    return best


def find_swing(doc: PdfDoc, page: int, x: float, y: float,
               expected_r: float | None = None,
               door_pt: float = 36.0) -> Swing | None:
    """The door swing nearest (x, y) on this page, in page points.

    Returns None when there is no arc there -- which is a real answer, not a
    failure: it means whatever the detector saw was not a door.
    """
    found = arcs_near(doc, page, x, y, expected_r, door_pt)
    if not found:
        return None

    # Agreement with the sheet's own door radius comes first, then the quality
    # of the fit, and only then distance.
    #
    # Nearest-first was the obvious rule and it was wrong. A window two door-
    # widths across also contains basins, chair backs and corner fillets, and
    # some of those sit closer to the detector's point than the door does: on
    # one sheet where every door is 3'-0" (r=27 pt), nearest-first confirmed 20
    # doors at r=19-20. What separates them is not position but precision -- a
    # CAD door arc fits a circle to 0.04 pt, the impostors to 0.17.
    def rank(arc: Swing) -> tuple[float, float, float]:
        off = abs(arc.radius - expected_r) / expected_r if expected_r else 0.0
        return (off, arc.residual, math.hypot(arc.hinge_x - x, arc.hinge_y - y))

    found.sort(key=rank)
    return found[0]


def leaves_at(doc: PdfDoc, page: int, x: float, y: float,
              expected_r: float | None = None,
              door_pt: float = 36.0) -> int:
    """How many door leaves turn about this one opening.

    One arc is a single door. Two arcs of the same radius, hinged about a
    door's width apart and swinging towards each other, are a pair -- one
    opening, two leaves, priced as a pair and not as two doors. Two arcs about
    the *same* hinge are a double-acting door, which swings both ways.

    Returned as a count so the caller can check what the model said the type
    was, rather than believing it. Nothing here is a guess: it is how many
    circles were fitted to the ink.
    """
    found = arcs_near(doc, page, x, y, expected_r, door_pt)
    if not found:
        return 0

    # This door's own leaf: the arc hinged nearest the point.
    mine = min(found, key=lambda a: math.hypot(a.hinge_x - x, a.hinge_y - y))
    radius = expected_r or mine.radius

    # A pair hangs two leaves in ONE opening, so its hinges sit at the two
    # jambs -- exactly one opening apart, which is two leaf-lengths.
    #
    # Counting instead every hinge within 1.6 leaf-lengths of the point got
    # this exactly backwards. Measured on one sheet where a leaf is 27 pt: a
    # real pair's second hinge stands 54 pt away and was excluded, while the
    # next door along stood 37-46 pt away and was counted. Four single doors
    # came back as pairs and no pair was ever found.
    return 2 if _partner(found, mine, radius) else 1


def _partner(found: list[Swing], mine: Swing, radius: float) -> Swing | None:
    """The other leaf of the same pair, if one hangs there."""
    for other in found:
        if other is mine:
            continue
        gap = math.hypot(other.hinge_x - mine.hinge_x,
                         other.hinge_y - mine.hinge_y)
        same_size = abs(other.radius - mine.radius) <= mine.radius * _LEAF_MATCH
        if same_size and abs(gap - 2 * radius) <= radius * _PAIR_TOLERANCE:
            return other
    return None


def other_leaf(doc: PdfDoc, page: int, mine: Swing,
               expected_r: float | None = None,
               door_pt: float = 36.0) -> Swing | None:
    """A pair's second leaf, measured like the first.

    Knowing a door is a pair and then drawing one leaf of it is half an answer:
    on screen a six-foot opening looked like a three-foot door with the wrong
    label. Both leaves are in the drawing and both were being fitted -- only
    one was ever kept.
    """
    # Look wider than usual. The partner hinges two leaves away and its arc
    # sweeps a further leaf beyond that, so the ordinary window clips the chain
    # and the clipped chain fails the quarter-turn test.
    found = arcs_near(doc, page, mine.hinge_x, mine.hinge_y, expected_r,
                      door_pt * _PARTNER_WINDOW)
    return _partner(found, mine, expected_r or mine.radius)


def arcs_for_tags(doc: PdfDoc, page: int, points: list[tuple[float, float]],
                  expected_r: float | None = None,
                  door_pt: float = 36.0) -> list[Swing | None]:
    """Give each door number the arc that belongs to it, however far it sits.

    A fixed reach cannot work. One sheet prints the number at the opening --
    half a leaf from the hinge -- and the next prints it out on a leader line
    two or three leaves away. Cap it short and the leadered sheet gets nothing;
    cap it long and a door takes the swing of the door beside it, which is how
    B101 came to be drawn on B102's door.

    Distance is the wrong question. The right one is whose arc it is: a number
    may claim an arc only when that arc's own nearest number is this one. Then
    the reach can be as generous as you like, because a far arc that belongs to
    somebody nearer is refused on those grounds rather than on inches.

    Everything else stays as it was -- radius agreement first, so basins and
    chair backs never enter, and one arc to one door.
    """
    reach = door_pt * _TAG_SWEEP
    arcs: dict[tuple[int, int], Swing] = {}
    for x, y in points:
        for arc in arcs_near(doc, page, x, y, expected_r, reach / _WINDOW):
            arcs.setdefault((round(arc.hinge_x / _HINGE_TOL),
                             round(arc.hinge_y / _HINGE_TOL)), arc)

    if not arcs:
        return [None] * len(points)

    def gap(arc: Swing, point: tuple[float, float]) -> float:
        return math.hypot(arc.hinge_x - point[0], arc.hinge_y - point[1])

    # Let each arc name its own door, rather than letting each door reach for an
    # arc. This is the rule this function was written around and it was not the
    # rule being run: the code below used to offer every door every arc within
    # six leaves and settle the clashes by augmenting path, so a door with no
    # arc of its own would take a distant one and push its neighbours further
    # out again. Measured on a 36-door sheet, that put twelve doors on their own
    # arc at half a leaf and smeared the other twenty-three evenly out to the
    # six-leaf ceiling -- the shape of everybody taking whatever was left.
    #
    # An arc belongs to the door nearest it. Nothing else can claim it, however
    # much that other door would like an arc, because a door being empty-handed
    # is not evidence about somebody else's swing. A door left with nothing here
    # is a door whose swing was not drawn, or not found -- which is a true
    # answer, and a better one than a neighbour's arc wearing its number.
    owned: dict[int, tuple[float, tuple[int, int]]] = {}
    for key, arc in arcs.items():
        near = min(range(len(points)), key=lambda i: gap(arc, points[i]))
        away = gap(arc, points[near])
        if away > reach:
            continue
        if near not in owned or away < owned[near][0]:
            owned[near] = (away, key)

    out: list[Swing | None] = [None] * len(points)
    for i, (_away, key) in owned.items():
        out[i] = arcs[key]

    # A sheet numbers its doors one way throughout. Where it prints the number
    # at the opening every door's arc is about half a leaf away; where it
    # prints on leader lines every door's is two or three. So the sheet says
    # what normal is, and a door far outside its own sheet's habit is not
    # measured, it is guessed at.
    #
    # Measured: one enlarged plan puts eight of its nine doors at 0.3-0.5
    # leaves and one at 4.5. The 4.5 is the odd one out on that sheet, and on
    # another sheet 2.0-3.6 is every single door and perfectly normal.
    gaps = sorted(gap(s, points[i]) for i, s in enumerate(out) if s)
    if len(gaps) >= _MIN_HABIT:
        # The lower quarter, not the middle. The median assumes most of the
        # assignments are right, and when they are not it sits inside the wrong
        # group and blesses it: on one 36-door sheet twelve doors sat at 0.5
        # leaves and the other twenty-three smeared evenly out to the 6-leaf
        # search ceiling -- the shape of "took whatever was left". The median of
        # that is 3.2 leaves, so the limit came out at 9.6 and the filter could
        # never fire on anything.
        #
        # A quarter of the doors agreeing is a habit; it is also the most that
        # can be wrong before the number stops meaning anything. Where a sheet
        # really does letter every door on a leader, every gap is large, the
        # quarter mark is large with them and nothing is dropped.
        usual = gaps[len(gaps) // 4]
        limit = max(usual * _GAP_OUTLIER, door_pt)
        for i, swing in enumerate(out):
            if swing and gap(swing, points[i]) > limit:
                log.info("swing_finder: page %s, an arc %.0f pt from its "
                         "number when this sheet's habit is %.0f pt -- not "
                         "its own", page, gap(swing, points[i]), usual)
                out[i] = None

    log.info("swing_finder: page %s, %d numbers and %d arcs -> %d agreed on "
             "both sides", page, len(points), len(arcs),
             sum(1 for s in out if s))
    return out


def assign_swings(doc: PdfDoc, page: int, points: list[tuple[float, float]],
                  expected_r: float | None = None,
                  door_pt: float = 36.0) -> list[Swing | None]:
    """The swing for each point, with no two points given the same one.

    `find_swing` answers for one point at a time, and that is not enough. Doors
    on a real plan stand about 36 pt apart while the search window is a couple
    of door-widths, so two neighbouring points see the same two arcs -- and
    since both arcs are struck at the same radius and fit just as tightly,
    nothing in a single-point ranking separates them. Both points then pick the
    same arc. Measured on one sheet: 33 of 36 points found an arc, and those 33
    resolved to 21 distinct doors. Twelve real doors had their swing taken by a
    neighbour and were reported as not drawn.

    So the arcs are handed out once, as a set. Radius agreement still leads --
    that is what keeps basins and chair backs out -- and among arcs that agree
    equally well, which is every genuine door on a sheet, position decides.

    Taking the closest pair first and moving on is not good enough, and the way
    it fails is worth knowing. A point in the middle of a run of doors sees
    four arcs and is spoilt for choice; a point at the end of the run sees only
    its own. Settle the easy one first and it can take the only arc the other
    had, leaving a real door with nothing -- on the same sheet that cost six
    doors out of thirty-one.

    So a point that has lost its arc is allowed to ask for it back: the point
    holding it looks for another it can move to, and if one exists both are
    served. That is an augmenting path, and running it to exhaustion places
    every door that can be placed -- no door is lost merely to the order the
    arcs happened to be considered in.

    Returns one entry per point, in order, `None` where nothing was left for it.
    """
    pools = [arcs_near(doc, page, x, y, expected_r, door_pt) for x, y in points]

    # The same arc reached from two points is one arc. Its hinge is exact, so
    # it is the identity; the alternative is comparing floats that were fitted
    # separately and differ in the last place.
    arcs: dict[tuple[int, int], Swing] = {}
    wanted: list[list[tuple[int, int]]] = []
    for index, pool in enumerate(pools):
        x, y = points[index]
        ranked: list[tuple[float, float, float, tuple[int, int]]] = []
        for arc in pool:
            key = (round(arc.hinge_x / _HINGE_TOL), round(arc.hinge_y / _HINGE_TOL))
            arcs.setdefault(key, arc)
            off = abs(arc.radius - expected_r) / expected_r if expected_r else 0.0
            ranked.append((round(off, 2),
                           math.hypot(arc.hinge_x - x, arc.hinge_y - y),
                           arc.residual, key))
        ranked.sort()
        wanted.append([key for *_score, key in ranked])

    holder: dict[tuple[int, int], int] = {}  # arc -> the point holding it

    def claim(index: int, asked: set[tuple[int, int]]) -> bool:
        """Give this point an arc, moving whoever holds its choices if it can."""
        for key in wanted[index]:
            if key in asked:
                continue
            asked.add(key)
            if key not in holder or claim(holder[key], asked):
                holder[key] = index
                return True
        return False

    # Hardest first: a point with one choice must be served before a point with
    # four takes it. The augmenting path would recover from that anyway, but
    # starting here means it rarely has to.
    for index in sorted(range(len(points)), key=lambda i: len(wanted[i])):
        claim(index, set())

    out: list[Swing | None] = [None] * len(points)
    for key, index in holder.items():
        out[index] = arcs[key]

    log.info("swing_finder: %d points, %d distinct arcs reachable, %d placed",
             len(points), len(arcs), sum(1 for s in out if s is not None))
    return out


def calibrate(doc: PdfDoc, page: int, points: list[tuple[float, float]],
              door_pt: float = 36.0) -> float | None:
    """The swing radius doors are actually drawn at on this sheet.

    Learned from the sheet rather than hard-coded, for the same reason the door
    tag's font size is: the next drawing set is at another scale and any number
    written here would be wrong for it. On one real sheet this returns 27.0 pt,
    which is a 3'-0" door at 1/8" = 1'-0" -- so it also recovers the drawing's
    scale as a side effect.

    A door's arc appears about ONCE PER DOOR. Everything else that curves --
    corner fillets, rounded joints, furniture -- appears everywhere. So a
    candidate radius is judged by how many *distinct* doors it explains, and
    the largest radius that explains most of them wins: a fillet can sit inside
    a door, but a door is never a detail inside a fillet.

    Counting arcs instead of doors is what this used to do, and it failed
    completely on a sheet drawn at 1/16" scale. Near its 65 doors there are 208
    arcs of radius 1 pt and 46 of radius 13 -- the rounded corners outvoted the
    doors four to one, calibration returned 0.9 pt, and the radius filter then
    rejected every real door on the sheet. Measured on that page: 23 of 65
    doors confirmed at 0.9 pt, 55 of 65 at the true 13.5.

    Deliberately free of any assumed scale. Two of the six sets are drawn at
    1/8", one at 1/16"; nothing here needs to know which.
    """
    sample = points[:_CALIBRATION_SAMPLE]
    if not sample:
        return None

    covers: dict[float, set[int]] = defaultdict(set)
    for index, (x, y) in enumerate(sample):
        for arc in arcs_near(doc, page, x, y, expected_r=None, door_pt=door_pt):
            covers[round(arc.radius * 2) / 2].add(index)

    # Below this a curve is a corner, not a door, at any drawing scale a set is
    # ever printed at: 3 pt is a 4-inch leaf even at 1/16".
    usable = {r: d for r, d in covers.items() if r >= _MIN_DOOR_RADIUS}
    if not usable:
        log.info("swing_finder: no door-sized arcs near the doors on page %s; "
                 "not calibrating", page)
        return None

    best = max(len(d) for d in usable.values())
    if best < _MIN_CALIBRATION:
        log.info("swing_finder: only %d door(s) on page %s share a radius; "
                 "not calibrating", best, page)
        return None

    # Among the radii that explain nearly as many doors as the best one, take
    # the largest -- the smaller ones are its own corners and joints.
    good = [r for r, d in usable.items() if len(d) >= best * _AGREEMENT]
    typical = max(good)
    log.info("swing_finder: page %s draws doors at r=%.1f pt "
             "(seen at %d of %d doors sampled)", page, typical,
             len(usable[typical]), len(sample))
    return typical


# Enough doors to be sure of the answer without walking the whole sheet. Higher
# than it was, because the test is now "how many doors share this radius" and
# twelve samples is a thin vote.
_CALIBRATION_SAMPLE = 40
# How many doors must share a radius before it is a measurement.
_MIN_CALIBRATION = 3
# Under this many points a curve is a corner or a joint, not a door leaf, at
# any scale a drawing set is printed at -- 3 pt is a four-inch door at 1/16".
_MIN_DOOR_RADIUS = 3.0
# A radius explaining this share of the best one's doors is a rival worth
# preferring if it is larger.
_AGREEMENT = 0.8

# A door of one of these types has no arc, so finding none proves nothing about
# it. Only a door the detector called a *swinging* one can be condemned for
# having no swing.
SWINGING = frozenset({"single_swing", "double_swing", "double_acting"})


def swings(door_type: str) -> bool:
    return (door_type or "").strip().lower() in SWINGING
