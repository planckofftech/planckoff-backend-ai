"""Tell a door's number from a room's, from how this set prints the two.

Runs after `door_locator.locate` on a plan read of the whole set (see
`apply`, called from `plan_pipeline.audit`, behind settings.room_tag_judge).
It only ever overrules `locate` where `locate` picked a room label: there the
pick moves to the door's own tag, or is left for a person to place. Where
`locate` already sits on a door tag, its answer stands untouched -- measured
on the testers' corrections, that rule broke no door that was right.

Why it exists
-------------
`locate` scores each printing of a number with fixed points -- the only one
on the sheet, under a word, beside a dimension -- and keeps one winner per
sheet. Fixed points mean different things on different sets. On one set "the
only 101 on the sheet" is the door; on the next it is the room, because that
sheet prints room numbers and no door numbers at all. The winner is then a
room number, reported with the highest confidence there is.

The room label is the key
-------------------------
A room is labelled with a name and a number printed together: WOMENS over
113, TOILET over 108, Collaboration over 958. Each name belongs to exactly one
number -- the one it is printed with -- so names and numbers are paired off
one to one, each with its own nearest, and only where each is the other's
nearest. That is what stops a door bubble standing beside a room's label being
taken for the room: "Collaboration" sits nearer 958 than 954, so 954 stays a
door. A printing paired with a name is a room label. Anything left is a
candidate for a door's own tag.

Two kinds of set
----------------
  tags    the set prints a tag at each door. The door is the unpaired
          printing; a door seen only as a room label has no tag on that sheet,
          and is not placed on the label -- it is sent to a person.
  rooms   the set prints no door tags at all: nearly every printing is a room
          label, and a door is known by the room it opens into (Fifth Third's
          ENTRY 101, ALLSUP'S COOLER A 107). The door is then the opening that
          belongs to that label -- the arc nearest it that no other label is
          nearer.

The set says which it is: the share of its printings that are room labels.

Among unpaired printings, the set's habit
-----------------------------------------
Where several unpaired printings compete, the ones with a door drawn at them
show what this set's door tags look like -- bubbled or bare, size, font,
colour, on the wall or not. A habit counts only where most of those agree and
the room labels do not, so a feature that separates nothing on this set is
ignored on this set. Nothing is written down in advance and nothing is carried
between sets.

Where the set gives nothing to judge by -- no labels to pair, no doors drawn
at its numbers -- the answer is `defer`: keep what `locate` said.

Nothing here logs a reason or a score.
"""

from __future__ import annotations

import bisect
import logging
import math
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from app.core import stacked_tags, swing_finder, wall_tags
from app.core.pdf_doc import PdfDoc, TextItem
from app.core.plan_index import SHEET_NUMBER, PlanSheet

log = logging.getLogger(__name__)

# A leaf length to search with where the sheet's own could not be measured.
_BASE_DOOR_PT = 36.0
# Unpaired printings with a door drawn at them, needed before a habit counts.
_MIN_SURE_DOORS = 5
# Room labels needed before they are used to test a habit.
_MIN_SURE_ROOMS = 3
# A habit: this share of the sure doors agree on it...
_HABIT_SHARE = 0.8
# ...and no more than this share of the room labels do.
_ROOM_SHARE = 0.5
# A room-label layout ("number under the name, upright") counts as this set's
# when at least this share of its labels are laid out that way.
_LAYOUT_SHARE = 0.25
# At or above this share of printings being room labels, the set prints no
# door tags and names its doors after their rooms.
_ROOMS_SET = 0.75
# A sheet where under this share of the printings are door tags prints its
# doors by their rooms' numbers, whatever the rest of the set does: an overall
# plan beside enlarged ones. Its picks stand. Measured 27 November 2026:
# Harmony's overall plans A-101 and A-102 at 10% and 3%, DISD's at 0%; the
# lowest sheet whose room-label picks testers moved by hand, Fifth Third's, at
# 23%; every other sheet the judge changed at 45% or over.
_DOOR_SHEET = 0.15
# Label geometry, in multiples of the number's own text height.
_STACK_GAP = 1.2       # edge to edge, above or below
_STACK_SLACK = 0.5     # horizontal slack past the wider of the two
_BESIDE_GAP = 1.5      # edge to edge, on the same line
_SAME_LINE = 0.5       # centre heights within this
_OVERLAP = 0.3         # edges may overlap by this much
# A leader starts at the number: within this many text heights of its centre,
# or of its mark's edge where it has one...
_LEADER_START = 1.5
# ...is at least this many leaves long and at most this many...
_LEADER_MIN, _LEADER_MAX = 0.6, 6.0
# ...and ends within this many leaves of the door it labels.
_LEADER_REACH = 1.5
# A wall: a line at least this many leaves long, passing within this many
# text heights of the number's centre.
_WALL_LEAVES = 2.0
_WALL_NEAR = 1.5
# Words that sit beside numbers as notes, never as a room's name.
_NOT_A_NAME = frozenset({
    "TYP", "TYP.", "SIM", "SIM.", "TYPICAL", "SIMILAR", "EXIST", "EXIST.",
    "EXISTING", "NIC", "N.I.C.", "ALIGN", "CLR", "CLR.", "MIN", "MIN.", "MAX",
    "MAX.", "EQ", "EQ.", "UP", "DN", "OPP", "OPP.", "HAND", "PAIR",
})
# A number as a room or a door is numbered: short, with a digit in it.
_NUMBERISH = re.compile(r"^[A-Z]{0,3}[-.]?\d[\dA-Z.\-]{0,6}$", re.IGNORECASE)


@dataclass(slots=True)
class Printing:
    """One place a scheduled number is printed on a plan sheet."""

    tag: str
    page: int
    sheet: str
    item: TextItem
    shape: str = ""
    opening: str = ""            # "arc" | "leader" | "leaf" | ""
    arc: swing_finder.Swing | None = None
    label: bool = False          # paired with a room name
    # How it stands against that name -- "below", "above", "right", "left" --
    # and whether it reads upright: "below/h". A set lays its room labels out
    # one way, and a pair laid out another way is a coincidence.
    label_dir: str = ""
    on_wall: bool = False
    door_pt: float = _BASE_DOOR_PT  # the sheet's leaf length, measured where it could be
    kind: str = ""               # "door" | "room_door" | "room" | "unclear"
    hits: int = 0                # habits this printing matches
    misses: int = 0              # habits it does not

    @property
    def door_drawn(self) -> bool:
        return self.opening in ("arc", "leader")

    def style(self, name: str):
        if name == "shape":
            return self.shape
        if name == "size":
            return round(self.item.size, 1)
        if name == "font":
            return self.item.font
        if name == "color":
            return self.item.color
        if name == "on_wall":
            return self.on_wall
        raise KeyError(name)


_STYLES = ("shape", "size", "font", "color", "on_wall")


@dataclass(slots=True)
class Habit:
    """What this set's door numbers look like, learned from the set itself."""

    mode: str = "tags"           # "tags" | "rooms"
    label_share: float = 0.0
    styles: dict[str, object] = field(default_factory=dict)
    sure_doors: int = 0
    room_labels: int = 0


@dataclass(slots=True)
class Verdict:
    """The answer for one door number."""

    tag: str
    status: str                  # "placed" | "check" | "defer" | "not_found"
    picks: list[Printing] = field(default_factory=list)   # one per sheet at most
    printings: list[Printing] = field(default_factory=list)
    # Sheets where today's answer stands, untouched: it already sits on a
    # printing this judge also takes for a door.
    kept: list[int] = field(default_factory=list)


# ---------------------------------------------------------------- measuring --

def _height(item: TextItem) -> float:
    """The text's own scale. The font size, not the box: a number printed on
    its side reports a box as tall as the number is long, and judging reach by
    that let CHI's sideways door bubble 135A claim the room name under it."""
    return max(item.size or (item.y1 - item.y0), 0.1)


def _is_name(word: TextItem, sheet_numbers: set[str] | None) -> bool:
    text = word.text.strip()
    if len(text) < 3 or sum(c.isalpha() for c in text) < 3:
        return False
    if text.upper() in _NOT_A_NAME:
        return False
    if any(c.isdigit() for c in text) and ("'" in text or '"' in text):
        return False  # a dimension
    if SHEET_NUMBER.match(text) and ("." in text or (sheet_numbers
                                                     and text in sheet_numbers)):
        return False  # a callout to another sheet
    return True


def _is_number(item: TextItem) -> bool:
    text = item.text.strip()
    return bool(_NUMBERISH.match(text)) and "'" not in text and '"' not in text


def _labels_together(number: TextItem, word: TextItem) -> str:
    """Where the number stands against the name, if the two are printed as
    one label -- "below", "above", "right", "left" -- or "" if they are not.

    In multiples of the number's own size, so a label set large on a 1/4"
    plan and one set small on a 1/16" plan are judged alike.
    """
    h = _height(number)
    slack = max(word.x1 - word.x0, number.x1 - number.x0) / 2 + h * _STACK_SLACK
    if abs(word.cx - number.cx) <= slack:
        if -_OVERLAP * h <= number.y0 - word.y1 <= _STACK_GAP * h:
            return "below"
        if -_OVERLAP * h <= word.y0 - number.y1 <= _STACK_GAP * h:
            return "above"
    if abs(word.cy - number.cy) <= _SAME_LINE * h:
        if -_OVERLAP * h <= number.x0 - word.x1 <= _BESIDE_GAP * h:
            return "right"
        if -_OVERLAP * h <= word.x0 - number.x1 <= _BESIDE_GAP * h:
            return "left"
    return ""


# Words of one name: lines this close, edge to edge, in multiples of the
# text's height, and words on one line this close.
_NAME_LINE_GAP = 0.6
_NAME_WORD_GAP = 1.0


def _name_blocks(names: list[TextItem]) -> list[TextItem]:
    """Words that make one room name -- stacked lines or words on a line set
    close together, at one size -- joined into one name spanning them all."""
    if len(names) < 2:
        return names
    parent = list(range(len(names)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    # Each word is tested only against the words in the cells around it. A
    # scan down the sheet by height alone tested every word on a crowded
    # sheet's line against every other: one set's plan read went from a minute
    # to thirty-five.
    # Sized to the usual word, not the largest: one title in huge letters must
    # not make every cell the size of the sheet.
    heights = sorted(_height(w) for w in names)
    cell = max(4 * heights[len(heights) // 2], 1.0)
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, w in enumerate(names):
        for gx in range(int(w.x0 // cell), int(w.x1 // cell) + 1):
            for gy in range(int(w.y0 // cell), int(w.y1 // cell) + 1):
                grid[(gx, gy)].append(i)
    for i, a in enumerate(names):
        h = _height(a)
        reach = h * (1 + _NAME_LINE_GAP + _NAME_WORD_GAP)
        near: set[int] = set()
        for gx in range(int((a.x0 - reach) // cell), int((a.x1 + reach) // cell) + 1):
            for gy in range(int((a.y0 - reach) // cell), int((a.y1 + reach) // cell) + 1):
                near.update(grid.get((gx, gy), ()))
        for j in near:
            if j <= i:
                continue
            # The upper of the two first, its size the measure, as a walk
            # down the sheet meets them.
            up, down = (names[j], a) if names[j].cy < a.cy else (a, names[j])
            hu = _height(up)
            if abs(_height(down) - hu) > 0.15 * hu:
                continue
            overlap_x = min(up.x1, down.x1) - max(up.x0, down.x0)
            stacked = overlap_x > 0 and 0 <= down.y0 - up.y1 <= _NAME_LINE_GAP * hu
            same_line = (abs(up.cy - down.cy) <= 0.3 * hu
                         and min(abs(down.x0 - up.x1), abs(up.x0 - down.x1))
                         <= _NAME_WORD_GAP * hu)
            if stacked or same_line:
                parent[root(j)] = root(i)
    groups: dict[int, list[TextItem]] = defaultdict(list)
    for i, w in enumerate(names):
        groups[root(i)].append(w)
    out = []
    for words in groups.values():
        if len(words) == 1:
            out.append(words[0])
            continue
        words.sort(key=lambda w: (round(w.cy), w.x0))
        first = words[0]
        out.append(TextItem(min(w.x0 for w in words), min(w.y0 for w in words),
                            max(w.x1 for w in words), max(w.y1 for w in words),
                            " ".join(w.text.strip() for w in words), first.size,
                            first.horizontal, first.font, first.color))
    return out


def paired_with_a_name(found: list[TextItem], numbers: list[TextItem],
                       names: list[TextItem]) -> dict[int, str]:
    """Which of `found` are a room's number, and how each stands against its
    name: id -> "below" | "above" | "right" | "left".

    `numbers` is every number-like printing on the sheet -- room numbers that
    are no door's included, because they compete for the names too. A name
    goes to its nearest number, a number to its nearest name, and only a
    mutual choice is a pair.
    """
    def gap(a: TextItem, b: TextItem) -> float:
        return math.hypot(a.cx - b.cx, a.cy - b.cy)

    # A room's name printed over two lines is one name. Midkiff prints JANITOR
    # over CLOSET over 122; taken word by word, CLOSET claimed 122 and JANITOR
    # claimed the nearest number left -- door bubble 122A just above it -- and
    # the door was called a room.
    names = _name_blocks(names)
    # Names sorted by height on the sheet, so each number only tests the few
    # within label reach of it rather than every word on a crowded sheet.
    by_y = sorted(names, key=lambda w: w.cy)
    ys = [w.cy for w in by_y]
    name_of: dict[int, TextItem] = {}
    for n in numbers:
        reach = _height(n) * (_STACK_GAP + 4)
        lo, hi = bisect.bisect_left(ys, n.cy - reach), bisect.bisect_right(ys, n.cy + reach)
        near = [w for w in by_y[lo:hi] if w is not n and _labels_together(n, w)]
        if near:
            name_of[id(n)] = min(near, key=lambda w: gap(n, w))
    number_of: dict[int, TextItem] = {}
    for n in numbers:
        w = name_of.get(id(n))
        if w is None:
            continue
        held = number_of.get(id(w))
        if held is None or gap(n, w) < gap(held, w):
            number_of[id(w)] = n
    return {id(n): _labels_together(n, name_of[id(n)]) for n in found
            if id(n) in name_of and number_of.get(id(name_of[id(n)])) is n}


def _distance_to_segment(px: float, py: float,
                         seg: tuple[float, float, float, float]) -> float:
    x0, y0, x1, y1 = seg
    dx, dy = x1 - x0, y1 - y0
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return math.hypot(px - x0, py - y0)
    t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / length2))
    return math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))


def _on_wall(doc: PdfDoc, page: int, item: TextItem, door_pt: float) -> bool:
    near = _height(item) * _WALL_NEAR
    shortest = door_pt * _WALL_LEAVES
    reach = near + 1.0
    for seg in doc.segments(page - 1, within=(item.cx - reach, item.cy - reach,
                                              item.cx + reach, item.cy + reach)):
        if math.hypot(seg[2] - seg[0], seg[3] - seg[1]) < shortest:
            continue
        if _distance_to_segment(item.cx, item.cy, seg) <= near:
            return True
    return False


def _leader_door(doc: PdfDoc, page: int, item: TextItem, mark_radius: float,
                 expected_r: float | None, door_pt: float,
                 tick=None) -> bool:
    """Does a line leave this number and end at a door?

    Stricter than `swing_finder.leader_end`: the line has to start at the
    number itself -- at its mark's edge, or within a text height or so of the
    glyphs -- and a door has to be drawn where it ends. A wall that merely
    starts somewhere near a number fails the first test; a dimension line that
    starts at a number fails the second.
    """
    start = max(mark_radius, _height(item)) * _LEADER_START
    shortest, longest = door_pt * _LEADER_MIN, door_pt * _LEADER_MAX
    for x0, y0, x1, y1 in doc.segments(page - 1, within=(item.cx - start, item.cy - start,
                                                         item.cx + start, item.cy + start)):
        for ax, ay, bx, by in ((x0, y0, x1, y1), (x1, y1, x0, y0)):
            if math.hypot(ax - item.cx, ay - item.cy) > start:
                continue
            length = math.hypot(bx - ax, by - ay)
            if not (shortest <= length <= longest):
                continue
            if tick is not None:
                tick()
            for a in swing_finder.arcs_near(doc, page, bx, by, expected_r, door_pt):
                if (math.hypot(a.hinge_x - bx, a.hinge_y - by) <= door_pt * _LEADER_REACH
                        or math.hypot((a.x0 + a.x1) / 2 - bx, (a.y0 + a.y1) / 2 - by)
                        <= door_pt * _LEADER_REACH):
                    return True
    return False


class OutOfTime(Exception):
    """The judge ran past its time on this set; today's answer stands."""


def measure(doc: PdfDoc, tags: set[str], sheets: list[PlanSheet],
            avoid: dict[int, list[tuple[float, float, float, float]]] | None = None,
            sheet_numbers: set[str] | None = None,
            deadline: float | None = None) -> list[Printing]:
    """Every printing of every scheduled number on these sheets, measured.

    `deadline` (time.monotonic) stops it part-way with OutOfTime: see `apply`.
    """
    def in_time() -> None:
        if deadline is not None and time.monotonic() > deadline:
            raise OutOfTime

    out: list[Printing] = []
    for sheet in sheets:
        in_time()
        items = [i for i in doc.text_items(sheet.page - 1) if i.text.strip()]
        blanked = (avoid or {}).get(sheet.page) or []
        if blanked:
            items = [i for i in items
                     if not any(b[0] <= i.x0 and i.x1 <= b[2]
                                and b[1] <= i.y0 and i.y1 <= b[3] for b in blanked)]
        items = stacked_tags.join(items, tags)
        found = [i for i in items if i.text.strip() in tags]
        if not found:
            continue
        names = [i for i in items if _is_name(i, sheet_numbers)]
        numbers = [i for i in items if _is_number(i) or i.text.strip() in tags]
        labelled = paired_with_a_name(found, numbers, names)

        points = [(i.cx, i.cy) for i in found]
        radius = swing_finder.calibrate(doc, sheet.page, points, tick=in_time)
        door_pt = radius or _BASE_DOOR_PT

        # Arcs go to door tags before room labels. A small room's own swing
        # stands nearer the middle of the room than the tag outside its door,
        # and handed out among every printing at once the room label took it.
        free = [i for i in found if id(i) not in labelled]
        rooms = [i for i in found if id(i) in labelled]
        arc_of: dict[int, swing_finder.Swing] = {}
        taken: set[tuple[int, int]] = set()
        for group in (free, rooms):
            if not group:
                continue
            in_time()
            arcs = swing_finder.arcs_for_tags(doc, sheet.page,
                                              [(i.cx, i.cy) for i in group],
                                              expected_r=radius, door_pt=door_pt,
                                              tick=in_time)
            for item, arc in zip(group, arcs):
                if arc is None:
                    continue
                key = (round(arc.hinge_x), round(arc.hinge_y))
                if key in taken:
                    continue
                taken.add(key)
                arc_of[id(item)] = arc

        for item in found:
            in_time()
            mark = wall_tags.enclosure(doc, sheet.page, item.cx, item.cy, _height(item))
            p = Printing(tag=item.text.strip(), page=sheet.page,
                         sheet=sheet.number or f"p{sheet.page}", item=item,
                         shape=mark.shape if mark else "", arc=arc_of.get(id(item)),
                         label=id(item) in labelled, door_pt=door_pt)
            if p.label:
                p.label_dir = f"{labelled[id(item)]}/{'h' if item.horizontal else 'v'}"
            if p.arc is not None:
                p.opening = "arc"
            elif not p.label and _leader_door(doc, sheet.page, item,
                                              mark.radius if mark else 0.0,
                                              radius, door_pt, tick=in_time):
                p.opening = "leader"
            elif swing_finder.find_leaf(doc, sheet.page, item.cx, item.cy,
                                        door_pt=door_pt) is not None:
                p.opening = "leaf"
            p.on_wall = _on_wall(doc, sheet.page, item, door_pt)
            out.append(p)
    return out


# ----------------------------------------------------------------- learning --

def _keep_true_labels(printings: list[Printing]) -> None:
    """Undo the pairings that do not look like this set's room labels.

    A set prints its room labels one way throughout: the number under the
    name, upright, boxed or bare. A pairing laid out another way is a door tag
    that happens to stand by a name -- CHI's sideways bubble 135A just above
    JANITOR KITCHEN, whose own number is boxed under it; Jimmy Evans' circled
    124A beside DOUBLE, a word of "DOUBLE OFFICE" over its boxed 124.
    """
    labels = [p for p in printings if p.label]
    if len(labels) < _MIN_SURE_ROOMS:
        return
    layouts = Counter(p.label_dir for p in labels)
    usual = {d for d, n in layouts.items() if n / len(labels) >= _LAYOUT_SHARE}
    for p in labels:
        if p.label_dir not in usual:
            p.label = False
    labels = [p for p in labels if p.label]
    if len(labels) < _MIN_SURE_ROOMS:
        return
    shape, seen = Counter(p.shape for p in labels).most_common(1)[0]
    if seen / len(labels) < _HABIT_SHARE:
        return
    for p in labels:
        if p.shape and p.shape != shape:
            p.label = False


def learn(printings: list[Printing]) -> Habit | None:
    """This set's habits, or None where it gives nothing to judge by."""
    if not printings:
        return None
    _keep_true_labels(printings)
    labels = [p for p in printings if p.label]
    habit = Habit(label_share=len(labels) / len(printings), room_labels=len(labels))
    habit.mode = "rooms" if habit.label_share >= _ROOMS_SET else "tags"

    sure = [p for p in printings if not p.label and p.door_drawn]
    habit.sure_doors = len(sure)
    if len(sure) >= _MIN_SURE_DOORS:
        for name in _STYLES:
            value, seen = Counter(p.style(name) for p in sure).most_common(1)[0]
            if seen / len(sure) < _HABIT_SHARE:
                continue  # the doors themselves disagree: no habit to test
            if len(labels) >= _MIN_SURE_ROOMS:
                shared = sum(1 for p in labels if p.style(name) == value) / len(labels)
                if shared > _ROOM_SHARE:
                    continue  # rooms look the same on this: it separates nothing
            habit.styles[name] = value

    if not labels and not habit.styles:
        return None  # nothing to pair, nothing learned: today's answer stands
    return habit


# ----------------------------------------------------------------- judging --

def _classify(p: Printing, habit: Habit) -> None:
    p.hits = sum(1 for name, value in habit.styles.items() if p.style(name) == value)
    p.misses = len(habit.styles) - p.hits
    if p.label:
        p.kind = "room_door" if (habit.mode == "rooms" and p.opening == "arc") else "room"
    elif p.door_drawn:
        p.kind = "door"
    elif not habit.styles or p.misses <= 1:
        # No name against it, and drawn the way this set draws its door
        # numbers, give or take one thing: TWU's 112 sits in an oval at its
        # door and differs from the habit only in standing clear of the wall.
        p.kind = "door" if p.hits or not habit.styles else "unclear"
    else:
        p.kind = "unclear"


def _rank(p: Printing) -> tuple:
    """Which of several door-like printings is the door.

    The set's own habit first: Martin County draws its door numbers in circles,
    and a square with an arc beside it lost door 124 to a circle without one
    when the arc was asked first. Then whether a door is drawn there -- an arc
    and a leader count alike, because two printings each with a door at them
    are two candidates for a person to choose between, not a contest of kinds.
    """
    return (-p.misses, p.door_drawn, p.on_wall, p.opening == "leaf")


def _todays(page_printings: list[Printing], at: tuple[float, float]) -> Printing | None:
    """The printing today's answer sits on: the one under its centre, else the
    nearest within a text height of it."""
    x, y = at
    best, gap = None, math.inf
    for p in page_printings:
        it = p.item
        d = math.hypot(max(it.x0 - x, 0, x - it.x1), max(it.y0 - y, 0, y - it.y1))
        if d < gap:
            best, gap = p, d
    if best is not None and gap <= _height(best.item):
        return best
    return None


def judge(printings: list[Printing], tags: set[str],
          today: dict[str, dict[int, tuple[float, float]]] | None = None,
          ) -> tuple[Habit | None, list[Verdict]]:
    """A verdict per door number.

    `today` is where today's answer put each number's printing, per page (the
    number's centre in page points). This judge exists for the room-tag
    problem only: where today already sits on a printing it also takes for a
    door, or on one it cannot find, today's answer stands for that sheet and
    the sheet is listed in `kept`. It overrules today only where today sits on
    a room label or on a printing it finds no door at.
    """
    today = today or {}
    by_tag: dict[str, list[Printing]] = defaultdict(list)
    for p in printings:
        by_tag[p.tag].append(p)

    habit = learn(printings)
    room_sheets: set[int] = set()
    if habit is not None and habit.mode == "tags":
        on_page: dict[int, Counter] = defaultdict(Counter)
        for p in printings:
            _classify(p, habit)
            on_page[p.page][p.kind == "door"] += 1
        room_sheets = {page for page, c in on_page.items()
                       if c[True] < _DOOR_SHEET * (c[True] + c[False])}
    elif habit is not None:
        for p in printings:
            _classify(p, habit)
    out: list[Verdict] = []
    for tag in sorted(tags):
        mine = by_tag.get(tag, [])
        if not mine:
            out.append(Verdict(tag, "not_found"))
            continue
        if habit is None:
            out.append(Verdict(tag, "defer", printings=mine))
            continue

        by_page: dict[int, list[Printing]] = defaultdict(list)
        for p in mine:
            by_page[p.page].append(p)
        picks, kept = [], []
        for page, page_printings in by_page.items():
            at = today.get(tag, {}).get(page)
            if page in room_sheets:
                # A sheet numbering its doors by their rooms: the plan read's
                # answer there stands, and nothing is added where it had none.
                if at is not None:
                    kept.append(page)
                continue
            if at is not None:
                theirs = _todays(page_printings, at)
                if theirs is None or theirs.kind == "door":
                    kept.append(page)
                    continue
            doors = sorted((p for p in page_printings if p.kind == "door"),
                           key=_rank, reverse=True)
            if len(doors) > 1 and _rank(doors[0]) == _rank(doors[1]):
                continue  # two that look equally like the door: a person decides
            if doors:
                picks.append(doors[0])
                continue
            # No door tag on this sheet. In a set that names its doors after
            # their rooms, the room's own opening is the door.
            via_room = [p for p in page_printings if p.kind == "room_door"]
            if len(via_room) == 1:
                picks.append(via_room[0])
        kept.sort()
        if picks:
            picks.sort(key=_rank, reverse=True)
            out.append(Verdict(tag, "placed", picks, mine, kept))
        elif kept and len(kept) == len(by_page):
            out.append(Verdict(tag, "defer", printings=mine, kept=kept))
        else:
            out.append(Verdict(tag, "check", printings=mine, kept=kept))
    return habit, out


# ----------------------------------------------------------------- applying --

def apply(doc: PdfDoc, sightings: list, tags: set[str], sheets: list[PlanSheet],
          avoid: dict[int, list[tuple[float, float, float, float]]] | None = None,
          sheet_numbers: set[str] | None = None,
          seconds: float | None = None) -> tuple[list, int]:
    """`locate`'s sightings, with every pick that is a room label put right.

    Per door, per sheet this judge measured:

      kept      `locate` picked a door tag (or a printing this judge cannot
                tell about): its candidate stands exactly as it was
      moved     `locate` picked a room label and the door's own tag is there:
                the candidate moves to the tag
      dropped   `locate` picked a room label and no door tag can be told
                apart: the candidate goes, and the door is reported as not
                found on that sheet, for a person to place
      added     `locate` found nothing on a sheet where the door's tag is

    Sheets this judge did not measure keep `locate`'s candidates. Returns the
    sightings and how many door-sheets were left for a person. Logs counts
    only, never why.

    `seconds` bounds the measuring. A sheet dense enough to take longer than
    that is left exactly as `locate` read it: the judge can only skip its
    improvement there, never make the answer worse.
    """
    from app.core.door_locator import Candidate, DoorSighting

    started = time.monotonic()
    try:
        printings = measure(doc, tags, sheets, avoid, sheet_numbers,
                            deadline=started + seconds if seconds else None)
    except OutOfTime:
        log.info("tag_evidence: stopped after %.0f s; the plan read's answer "
                 "stands", time.monotonic() - started)
        return sightings, 0
    today: dict[str, dict[int, tuple[float, float]]] = {}
    for s in sightings:
        for c in s.candidates:
            today.setdefault(s.tag, {}).setdefault(
                c.page, ((c.x0 + c.x1) / 2, (c.y0 + c.y1) / 2))
    habit, verdicts = judge(printings, tags, today)
    if habit is None:
        return sightings, 0
    by_tag = {v.tag: v for v in verdicts}

    out, moved, dropped, added = [], 0, 0, 0
    for s in sightings:
        v = by_tag.get(s.tag)
        if v is None or v.status in ("defer", "not_found"):
            out.append(s)
            continue
        judged = {p.page for p in v.printings}
        pick_on = {p.page: p for p in v.picks}
        candidates = []
        for c in s.candidates:
            if c.page in v.kept or c.page not in judged:
                candidates.append(c)
            elif c.page in pick_on:
                p = pick_on.pop(c.page)
                it = p.item
                candidates.append(Candidate(c.page, c.sheet, it.x0, it.y0, it.x1, it.y1,
                                            c.score, list(c.reasons), it.size))
                if (it.x0, it.y0) != (c.x0, c.y0):
                    moved += 1
            else:
                dropped += 1
        for page, p in sorted(pick_on.items()):
            it = p.item
            candidates.append(Candidate(page, p.sheet, it.x0, it.y0, it.x1, it.y1,
                                        size=it.size))
            added += 1
        if candidates:
            out.append(DoorSighting(s.tag, candidates[0].page, candidates[0].sheet,
                                    s.confidence if candidates[0].page == s.page
                                    else "resolved", candidates))
        else:
            out.append(DoorSighting(s.tag, 0, "", "not_found", []))
    log.info(
        "tag_evidence: %d door number(s), %d moved, %d left for a person, %d added",
        len(sightings), moved, dropped, added)
    return out, dropped
