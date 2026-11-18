"""Find each scheduled door on the floor plans.

No model is involved and none is needed. These drawings are vector PDFs, so a
door tag is real text with exact coordinates -- on the reference set all 65
scheduled door numbers were found this way, at no cost.

The whole difficulty is that a door number is a very ordinary string. `11` on a
floor plan might be the door, a dimension, a keynote, or -- most often -- the
room number, because door numbering usually follows room numbering. Three
observations separate them, and all three are measured from the drawing itself
rather than assumed:

  size    Where a sheet sets its door tags in one size we learn it from the
          tags themselves, instead of hard-coding a number that would be wrong
          on the next drawing set. It is the weakest of the three and not
          always true -- Austin OBGYN sets its tags at three sizes on one plan
          -- so the caller can turn it off; see `size_filter`.

  stack   A room number sits directly beneath its room name. `59` under `CREW`
          is a room; `59` beside `58` and `15' - 0"` is a door.

  company Door tags keep company with other door tags and with dimension
          strings, because they are placed along the wall line.

Where these still do not settle it, the sighting is returned as `ambiguous`
with every candidate attached. Reporting the doubt is the point -- a confidently
wrong location is worse than a flagged one.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from app.core import stacked_tags, wall_tags
from app.core.pdf_doc import PdfDoc, TextItem
from app.core.plan_index import SHEET_NUMBER, PlanSheet

log = logging.getLogger(__name__)

# A tag whose text contains a letter and appears once on the sheet is a door
# tag beyond reasonable doubt; those calibrate the rest.
_ANCHOR_MIN_LEN = 3
# Below this many sightings the mode is not a measurement, it is an accident.
_MIN_SIZE_SAMPLES = 4
# Candidates must be within this fraction of the calibrated tag size.
_SIZE_TOLERANCE = 0.12
# How far around a candidate to look for company (pt).
_NEIGHBOUR_X = 45.0
_NEIGHBOUR_Y = 28.0
# A room number sits under its room name: same column, a line or so below.
_STACK_X = 22.0
_STACK_Y = 26.0
# Why a candidate lost points for being a room's number.
_ROOM = "sits under a room name"
# Learning a page's door-tag mark: how many sure tags to read, and how many
# must agree before it counts.
_SHAPE_SAMPLES, _MIN_SHAPE_SAMPLES = 12, 3
# A dimension string carries feet or inch marks.
_DIMENSION = re.compile(r"[0-9]\s*['’\"]")


@dataclass(slots=True)
class Candidate:
    page: int
    sheet: str
    x0: float
    y0: float
    x1: float
    y1: float
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    # The printed size, kept to settle a tie -- see `_break_tie`.
    size: float = 0.0


@dataclass(slots=True)
class DoorSighting:
    tag: str
    page: int  # best candidate's page, 0 when not found
    sheet: str
    confidence: str  # "unique" | "resolved" | "ambiguous" | "not_found"
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.candidates)


def _tag_size(sheet_items: list[TextItem], tags: set[str]) -> float | None:
    """The size door tags are set in on this sheet, learned from the tags.

    Learned from the schedule's own numbers where they appear on the sheet: the
    size most of them share is the size a door tag is set in, and anything at a
    different size is something else that reads the same.

    It used to be learned from any unique run of text with a letter in it, on
    the assumption that door numbers look like `101A`. Plenty do not. One
    project numbers its doors 101 to 126 with no letters at all, so not one of
    them contributed; the size came from unrelated labels elsewhere on the
    sheet and landed on 12.6 pt while every real tag was 9.6 pt. The filter
    then rejected all 24 of them, and the audit fell back to reading the
    schedule's own table instead.

    Returns None when there is nothing to learn from, in which case no size
    filter is applied -- better several candidates than a filter built on a
    guess.

    A number under a room name is the room's, and does not vote. Victoria's
    second floor prints its room numbers at 12.5 pt under the room names and
    its door tags at 9.6 pt; the rooms outnumbered the doors, the sheet
    learned 12.5, and the filter then threw away every real door tag on it --
    201, 202 and 203 were placed on MECHANICAL, OFFICES and ELECTRICAL ROOM.
    """
    printed = [i for i in sheet_items if i.text.strip() in tags]
    matched = [i.size for i in printed
               if not _looks_like_a_room_number(i, sheet_items)]
    if len(matched) >= _MIN_SIZE_SAMPLES:
        # The mode, not the median: a handful of tag-shaped impostors at some
        # other size must not drag the answer between the two.
        return Counter(round(s, 1) for s in matched).most_common(1)[0][0]
    if len(matched) < len(printed):
        # Too few left once the rooms are out. The fallback below learns from
        # whatever labels the sheet has -- here the room names, at the rooms'
        # size -- so it would rebuild the very filter just avoided. No filter.
        return None

    counts = Counter(i.text.strip() for i in sheet_items)
    sizes = [
        i.size for i in sheet_items
        if counts[i.text.strip()] == 1
        and len(i.text.strip()) >= _ANCHOR_MIN_LEN
        and any(c.isalpha() for c in i.text.strip())
    ]
    if not sizes:
        return None
    sizes.sort()
    return sizes[len(sizes) // 2]


def _is_a_sheet_reference(text: str, sheet_numbers: set[str] | None) -> bool:
    """A sheet number, as a detail or finish callout prints it: `A-1.52`.

    Written with a dot, or one of this set's own sheets -- so a room called
    `WC-1` is still a room name.
    """
    if not SHEET_NUMBER.match(text):
        return False
    return "." in text or bool(sheet_numbers and text in sheet_numbers)


def _looks_like_a_room_number(item: TextItem, items: list[TextItem],
                              sheet_numbers: set[str] | None = None) -> bool:
    """Is this the number under a room name, rather than a door tag?

    Room labels are stacked -- the name, then the number directly beneath it,
    in the same column. Nothing else on a plan is laid out that way.

    A sheet reference is not a room name. CHI prints the callout `A-1.52`
    just above door tag 114, which then read as a room's number, tied with the
    real room 114 under EXAM-5, and lost to it on page order.
    """
    for other in items:
        if other is item or not other.text.strip():
            continue
        text = other.text.strip()
        if not any(c.isalpha() for c in text) or len(text) < 3:
            continue
        if _is_a_sheet_reference(text, sheet_numbers):
            continue
        if (abs(other.cx - item.cx) <= _STACK_X
                and 0 < item.y0 - other.y0 <= _STACK_Y):
            return True
    return False


def _in_a_reference_bubble(item: TextItem, items: list[TextItem],
                           sheet_numbers: set[str] | None = None) -> bool:
    """Is this the top half of a detail or section marker?

    Those are drawn as a circle split in two: the detail number above, the
    sheet it lives on below -- `4` over `A4.51`. The number alone is exactly
    door-tag shaped and set at the same size, and one such marker was reported
    as an unscheduled door.

    A wall-type or door-type label under a door tag is shaped like a sheet
    number too -- `A3`, `EW2` -- and took two real doors for bubbles, so the
    room number of the same name won. Where the set's own sheet numbers are
    known, the lower half has to be one of them, or be written the way only a
    sheet number is, with its separator (`A4.51`, `A-511`): a bubble can point
    at a sheet left out of this upload.
    """
    for other in items:
        text = other.text.strip()
        if not text or other is item or not SHEET_NUMBER.match(text):
            continue
        if (sheet_numbers and text not in sheet_numbers
                and not any(c in text for c in ".-")):
            continue
        if (abs(other.cx - item.cx) <= _STACK_X
                and 0 < other.y0 - item.y0 <= _STACK_Y):
            return True
    return False


def _company(item: TextItem, items: list[TextItem], tags: set[str]) -> tuple[int, list[str]]:
    """Reward a candidate for the things placed around it."""
    score, reasons = 0, []
    others = [
        o for o in items
        if o is not item and o.text.strip()
        and abs(o.cx - item.cx) <= _NEIGHBOUR_X
        and abs(o.cy - item.cy) <= _NEIGHBOUR_Y
    ]
    if any(o.text.strip() in tags for o in others):
        score += 2
        reasons.append("beside another door tag")
    if any(_DIMENSION.search(o.text) for o in others):
        score += 1
        reasons.append("beside a dimension")
    return score, reasons


def _break_tie(doc: PdfDoc, tied: list[Candidate], tag_size: float | None,
               usual_shape) -> Candidate | None:
    """One of several printings that scored the same on a page, or None.

    A score tie used to go to whichever printing came first on the page, and
    that was the room number as often as the door. Two things the drawing
    states settle most of them, asked in order and only while they single one
    printing out:

      size   the size this sheet sets its door tags in. Lightbridge's door 110
             is 8.5 pt like every other tag; the room 110 under DAYCARE #11 is
             9.0. The size filter lets both through -- it is a tolerance, not
             an identity -- so they tied.
      mark   the shape this sheet draws its door tags in. Grace prints 101A in
             a circle at the door and again in a diamond by the storefront,
             the same size; its door tags are circles.

    Where neither singles one out, the tie stands and is reported.
    """
    if tag_size is not None:
        same = [c for c in tied if abs(c.size - tag_size) < 0.05]
        if len(same) == 1:
            return same[0]
        if same:
            tied = same
    shape = usual_shape()
    if shape:
        same = [c for c in tied if _mark_of(doc, c) == shape]
        if len(same) == 1:
            return same[0]
    return None


def _mark_of(doc: PdfDoc, c: Candidate) -> str:
    """The shape drawn around a candidate, or "" if it cannot be read."""
    try:
        return wall_tags.enclosure_shape(doc, c.page, (c.x0 + c.x1) / 2,
                                         (c.y0 + c.y1) / 2, c.y1 - c.y0)
    except Exception:  # noqa: BLE001 - a mark we cannot read settles nothing
        return ""


def locate(doc: PdfDoc, tags: set[str], sheets: list[PlanSheet],
           avoid: dict[int, list[tuple[float, float, float, float]]] | None = None,
           size_filter: bool = True, shape: str = "",
           sheet_numbers: set[str] | None = None) -> list[DoorSighting]:
    """Place every tag on the given sheets. Text only -- no rendering, no AI.

    `avoid` lists rectangles per page that must not be searched, in page points.
    It exists for one case, and that case silently invented a perfect score:
    a small project prints its door schedule *on* the floor plan sheet. The
    schedule's own column of door numbers is then the easiest thing on the page
    to find, so every door was "located" -- all of them in a single column an
    inch from the left edge, stepping evenly down the table -- and the audit
    reported that the schedule and the drawings agreed. It had only ever
    compared the schedule with itself.

    `size_filter` is the calibrated font-size test, and the caller may turn it
    off. It is a hard reject rather than a score, and one drawing shows why
    that is a liability: on Austin OBGYN's A2.1 the door numbers are set at
    three sizes in one plan -- glyphs 8.8, 13.4 and 18.9 points tall -- so any
    single learned size is wrong for two thirds of them, and `_tag_size`
    survives there only because the schedule's own numbers outvote the rest.

    Where the caller has a stronger test than size -- the shape this set draws
    its door marks in -- this one is redundant, and should not get a second,
    quieter veto over the marks that test would have kept.

    `shape` is that stronger test: the mark this set draws its door numbers in.
    Given one, a number drawn in anything else is not a door and is not a
    candidate, and the size test stands down in its favour.

    `sheet_numbers` are the set's own sheet numbers, which tell a detail
    bubble's lower half from a type label -- see `_in_a_reference_bubble`.

    `shape` belongs here rather than downstream, and the reason is the one case the
    whole test exists for. This function returns the best candidate *per page*,
    so where a sheet prints the same number twice -- once in a circle at the
    door, once bare in the middle of the room -- only one of the two ever
    reaches the caller, chosen on a score that knows nothing about marks.
    Filtering afterwards then cannot help: on Austin OBGYN's A2.1, door 113's
    bare room number outscored its own circled door mark, so the sighting that
    survived to be tested was already the wrong one, and testing it merely
    threw the door away. Judged here, the circle is never in competition with
    the room number in the first place.
    """
    per_tag: dict[str, list[Candidate]] = defaultdict(list)
    # Per page, the size its door tags are set in -- learned whether or not
    # the size filter is on, because it also settles ties.
    learned: dict[int, float | None] = {}

    for sheet in sheets:
        items = [i for i in doc.text_items(sheet.page - 1) if i.text.strip()]
        blanked = (avoid or {}).get(sheet.page) or []
        if blanked:
            before = len(items)
            items = [
                i for i in items
                if not any(b[0] <= i.x0 and i.x1 <= b[2]
                           and b[1] <= i.y0 and i.y1 <= b[3] for b in blanked)
            ]
            log.info("door_locator: page %d, ignoring %d text items inside the "
                     "schedule's own %d table(s)", sheet.page,
                     before - len(items), len(blanked))
        # `101` over `A` is door 101A -- see stacked_tags.
        items = stacked_tags.join(items, tags)
        # A named mark stands the size test down on its own. Leaving the two
        # to be turned off separately is a trap: the size filter runs first, so
        # a caller who names the mark and forgets the other flag gets tags
        # rejected on a size that was never reliable, before the test they
        # actually asked for is ever reached.
        learned[sheet.page] = _tag_size(items, tags)
        size = learned[sheet.page] if (size_filter and not shape) else None
        counts = Counter(i.text.strip() for i in items)

        for item in items:
            text = item.text.strip()
            if text not in tags:
                continue
            if size is not None and abs(item.size - size) > size * _SIZE_TOLERANCE:
                continue
            if shape and wall_tags.enclosure_shape(
                    doc, sheet.page, item.cx, item.cy,
                    item.y1 - item.y0) != shape:
                continue

            candidate = Candidate(sheet.page, sheet.number,
                                  item.x0, item.y0, item.x1, item.y1,
                                  size=item.size)
            if counts[text] == 1:
                candidate.score += 3
                candidate.reasons.append("only occurrence on the sheet")
            if _looks_like_a_room_number(item, items, sheet_numbers):
                candidate.score -= 3
                candidate.reasons.append(_ROOM)
            if _in_a_reference_bubble(item, items, sheet_numbers):
                candidate.score -= 4
                candidate.reasons.append("sits above a sheet reference")
            bonus, why = _company(item, items, tags)
            candidate.score += bonus
            candidate.reasons.extend(why)
            per_tag[text].append(candidate)

    shapes: dict[int, str] = {}

    def usual_shape(page: int) -> str:
        """The mark this page draws its door tags in, learned from the numbers
        printed once on it -- those are door tags beyond doubt. Only worked out
        for a page that has a tie to settle: reading marks is not free."""
        if page not in shapes:
            sure = [c for cands in per_tag.values() for c in cands
                    if c.page == page and "only occurrence on the sheet" in c.reasons]
            seen = Counter(_mark_of(doc, c) for c in sure[:_SHAPE_SAMPLES])
            seen.pop("", None)
            best = seen.most_common(1)
            shapes[page] = (best[0][0] if best and best[0][1] >= _MIN_SHAPE_SAMPLES
                            else "")
        return shapes[page]

    out: list[DoorSighting] = []
    for tag in sorted(tags):
        found = per_tag.get(tag, [])
        if not found:
            out.append(DoorSighting(tag, 0, "", "not_found"))
            continue

        # Contention is a within-sheet question. The same door legitimately
        # appears on the overall plan *and* on the enlarged plan that covers
        # its part of the building -- two correct sightings, not a conflict.
        # Judging across sheets called every one of those ambiguous.
        by_sheet: dict[int, list[Candidate]] = defaultdict(list)
        for candidate in found:
            by_sheet[candidate.page].append(candidate)

        picked: list[Candidate] = []
        contested = False
        for page, page_candidates in by_sheet.items():
            ranked = sorted(page_candidates, key=lambda c: -c.score)
            if len(ranked) > 1 and ranked[0].score == ranked[1].score:
                tied = [c for c in ranked if c.score == ranked[0].score]
                winner = _break_tie(doc, tied, learned.get(page),
                                    lambda p=page: usual_shape(p))
                if winner is None:
                    contested = True
                else:
                    ranked = [winner] + [c for c in ranked if c is not winner]
            picked.append(ranked[0])

        # A tie between sheets goes to the one that is not a room number. On
        # one set the room 117, alone on its sheet, scored level with door 117
        # on the next sheet, and won by coming first.
        picked.sort(key=lambda c: (-c.score, _ROOM in c.reasons))
        best = picked[0]
        if contested:
            confidence = "ambiguous"
        elif any(len(v) > 1 for v in by_sheet.values()):
            confidence = "resolved"
        else:
            confidence = "unique"
        out.append(DoorSighting(tag, best.page, best.sheet, confidence, picked))

    tally = Counter(s.confidence for s in out)
    log.info("door_locator placed %d/%d tags: %s",
             sum(1 for s in out if s.found), len(out), dict(tally))
    return out
