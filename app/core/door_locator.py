"""Find each scheduled door on the floor plans.

No model is involved and none is needed. These drawings are vector PDFs, so a
door tag is real text with exact coordinates -- on the reference set all 65
scheduled door numbers were found this way, at no cost.

The whole difficulty is that a door number is a very ordinary string. `11` on a
floor plan might be the door, a dimension, a keynote, or -- most often -- the
room number, because door numbering usually follows room numbering. Three
observations separate them, and all three are measured from the drawing itself
rather than assumed:

  size    Door tags are set in one size. We learn it from the tags that cannot
          be anything else (`127A`, `46C` -- lettered, and appearing once on
          the sheet), instead of hard-coding a number that would be wrong on
          the next drawing set.

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
    """
    matched = [i.size for i in sheet_items if i.text.strip() in tags]
    if len(matched) >= _MIN_SIZE_SAMPLES:
        # The mode, not the median: a handful of tag-shaped impostors at some
        # other size must not drag the answer between the two.
        return Counter(round(s, 1) for s in matched).most_common(1)[0][0]

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


def _looks_like_a_room_number(item: TextItem, items: list[TextItem]) -> bool:
    """Is this the number under a room name, rather than a door tag?

    Room labels are stacked -- the name, then the number directly beneath it,
    in the same column. Nothing else on a plan is laid out that way.
    """
    for other in items:
        if other is item or not other.text.strip():
            continue
        text = other.text.strip()
        if not any(c.isalpha() for c in text) or len(text) < 3:
            continue
        if (abs(other.cx - item.cx) <= _STACK_X
                and 0 < item.y0 - other.y0 <= _STACK_Y):
            return True
    return False


def _in_a_reference_bubble(item: TextItem, items: list[TextItem]) -> bool:
    """Is this the top half of a detail or section marker?

    Those are drawn as a circle split in two: the detail number above, the
    sheet it lives on below -- `4` over `A4.51`. The number alone is exactly
    door-tag shaped and set at the same size, and one such marker was reported
    as an unscheduled door.
    """
    for other in items:
        text = other.text.strip()
        if not text or other is item or not SHEET_NUMBER.match(text):
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


def locate(doc: PdfDoc, tags: set[str], sheets: list[PlanSheet],
           avoid: dict[int, list[tuple[float, float, float, float]]] | None = None
           ) -> list[DoorSighting]:
    """Place every tag on the given sheets. Text only -- no rendering, no AI.

    `avoid` lists rectangles per page that must not be searched, in page points.
    It exists for one case, and that case silently invented a perfect score:
    a small project prints its door schedule *on* the floor plan sheet. The
    schedule's own column of door numbers is then the easiest thing on the page
    to find, so every door was "located" -- all of them in a single column an
    inch from the left edge, stepping evenly down the table -- and the audit
    reported that the schedule and the drawings agreed. It had only ever
    compared the schedule with itself.
    """
    per_tag: dict[str, list[Candidate]] = defaultdict(list)

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
        size = _tag_size(items, tags)
        counts = Counter(i.text.strip() for i in items)

        for item in items:
            text = item.text.strip()
            if text not in tags:
                continue
            if size is not None and abs(item.size - size) > size * _SIZE_TOLERANCE:
                continue

            candidate = Candidate(sheet.page, sheet.number,
                                  item.x0, item.y0, item.x1, item.y1)
            if counts[text] == 1:
                candidate.score += 3
                candidate.reasons.append("only occurrence on the sheet")
            if _looks_like_a_room_number(item, items):
                candidate.score -= 3
                candidate.reasons.append("sits under a room name")
            if _in_a_reference_bubble(item, items):
                candidate.score -= 4
                candidate.reasons.append("sits above a sheet reference")
            bonus, why = _company(item, items, tags)
            candidate.score += bonus
            candidate.reasons.extend(why)
            per_tag[text].append(candidate)

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
        for page_candidates in by_sheet.values():
            ranked = sorted(page_candidates, key=lambda c: -c.score)
            picked.append(ranked[0])
            if len(ranked) > 1 and ranked[0].score == ranked[1].score:
                contested = True

        picked.sort(key=lambda c: -c.score)
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
