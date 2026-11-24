"""What a person drew on a plan, turned into what the vision pass reads.

A person drags a box over part of a floor plan and expects the doors in it.
Whatever they drew is answered -- a box smaller than a door, one that covers the
whole sheet, five that overlap, one dragged right to left past the edge. None of
that is the person's mistake to correct; it is ours to read sensibly:

  backwards / past the edge   corners put in order and clipped to the page
  smaller than a door         widened around its centre so the reader sees the
                              door and its number, then only the doors that
                              touch what was drawn are kept
  most of the sheet           the whole sheet is read
  overlapping or touching     read once, as one box around them
  together most of the sheet  the whole sheet is read

Two boxes are kept for every part: `read`, what the vision pass is sent, and
`drawn`, what the person outlined. A door is the answer to a drawing when its
centre is in `read` and its box touches `drawn`.

Everything here is fractions of the page, as the viewer sends them; page sizes
in points are only needed to know how small "smaller than a door" is.
"""

from __future__ import annotations

from dataclasses import dataclass, field

Box = tuple[float, float, float, float]

# Smallest box worth sending, in points on the sheet: about six doors across at
# the 36 pt a door usually measures. Enough for a door, its swing and the
# number printed beside it. A starting figure, to be tuned from real reads.
MIN_SIDE_PT = 216.0

# Drawn parts covering this much of a sheet are a request to read the sheet.
WHOLE_SHEET = 0.80

# Boxes this close are one part of the sheet, in points: reading them apart
# would send the strip between them twice.
MERGE_GAP_PT = 36.0

WHOLE: Box = (0.0, 0.0, 1.0, 1.0)


@dataclass
class AreaPlan:
    """One part of one sheet to read, and the drawing it answers."""

    page: int
    read: Box
    drawn: list[Box] = field(default_factory=list)
    whole: bool = False

    def as_json(self) -> dict:
        return {"page": self.page, "read": list(self.read),
                "drawn": [list(b) for b in self.drawn], "whole": self.whole}

    @classmethod
    def from_json(cls, raw: dict) -> AreaPlan:
        return cls(page=int(raw["page"]), read=tuple(raw["read"]),
                   drawn=[tuple(b) for b in raw.get("drawn") or []],
                   whole=bool(raw.get("whole")))


def _clip(v: float) -> float:
    return min(1.0, max(0.0, float(v)))


def normalise(x0: float, y0: float, x1: float, y1: float) -> Box:
    """Corners in order and inside the page, whichever way it was dragged."""
    a, b = sorted((_clip(x0), _clip(x1)))
    c, d = sorted((_clip(y0), _clip(y1)))
    return (a, c, b, d)


def _area(box: Box) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _widen(lo: float, hi: float, need: float) -> tuple[float, float]:
    """Grow [lo, hi] to at least `need` about its centre, kept inside 0..1."""
    if hi - lo >= need:
        return lo, hi
    if need >= 1.0:
        return 0.0, 1.0
    mid = (lo + hi) / 2
    lo, hi = mid - need / 2, mid + need / 2
    if lo < 0.0:
        lo, hi = 0.0, need
    if hi > 1.0:
        lo, hi = 1.0 - need, 1.0
    return lo, hi


def padded(box: Box, width_pt: float, height_pt: float) -> Box:
    """A box at least big enough to show a door and its number."""
    need_x = MIN_SIDE_PT / width_pt if width_pt > 0 else 0.0
    need_y = MIN_SIDE_PT / height_pt if height_pt > 0 else 0.0
    x0, x1 = _widen(box[0], box[2], need_x)
    y0, y1 = _widen(box[1], box[3], need_y)
    return (x0, y0, x1, y1)


def _near(a: Box, b: Box, gap_x: float, gap_y: float) -> bool:
    return (a[0] - gap_x <= b[2] and b[0] - gap_x <= a[2]
            and a[1] - gap_y <= b[3] and b[1] - gap_y <= a[3])


def _union(a: Box, b: Box) -> Box:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def plan_page(page: int, drawn: list[Box], width_pt: float,
              height_pt: float) -> list[AreaPlan]:
    """The parts of one sheet to read for everything drawn on it."""
    if not drawn:
        return []
    if any(_area(b) >= WHOLE_SHEET for b in drawn):
        return [AreaPlan(page, WHOLE, list(drawn), whole=True)]

    gap_x = MERGE_GAP_PT / width_pt if width_pt > 0 else 0.0
    gap_y = MERGE_GAP_PT / height_pt if height_pt > 0 else 0.0
    parts = [AreaPlan(page, padded(b, width_pt, height_pt), [b]) for b in drawn]
    merged = True
    while merged:
        merged = False
        for i in range(len(parts)):
            for j in range(i + 1, len(parts)):
                if _near(parts[i].read, parts[j].read, gap_x, gap_y):
                    parts[i] = AreaPlan(page, _union(parts[i].read, parts[j].read),
                                        parts[i].drawn + parts[j].drawn)
                    del parts[j]
                    merged = True
                    break
            if merged:
                break

    if sum(_area(p.read) for p in parts) >= WHOLE_SHEET:
        return [AreaPlan(page, WHOLE, list(drawn), whole=True)]
    return parts


def plan_areas(drawn: list[tuple[int, Box]],
               sizes: dict[int, tuple[float, float]]) -> list[AreaPlan]:
    """Every part to read, page by page, for everything drawn in one request."""
    by_page: dict[int, list[Box]] = {}
    for page, box in drawn:
        by_page.setdefault(page, []).append(box)
    out: list[AreaPlan] = []
    for page in sorted(by_page):
        w, h = sizes.get(page, (0.0, 0.0))
        out.extend(plan_page(page, by_page[page], w, h))
    return out


def _touches(box: Box, drawn: Box) -> bool:
    if _area(drawn) == 0.0:
        # A click rather than a drag: the door under the point.
        return box[0] <= drawn[0] <= box[2] and box[1] <= drawn[1] <= box[3]
    return box[0] < drawn[2] and drawn[0] < box[2] and box[1] < drawn[3] and drawn[1] < box[3]


def answers(page: int, box: Box, plans: list[AreaPlan]) -> bool:
    """Is a door at `box` on `page` part of what these plans were drawn for?"""
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    for p in plans:
        if p.page != page:
            continue
        if p.whole:
            return True
        r = p.read
        if r[0] <= cx <= r[2] and r[1] <= cy <= r[3] and any(
                _touches(box, d) for d in p.drawn):
            return True
    return False


def keep(found: list, plans: list[AreaPlan]) -> list:
    """The doors read that answer the drawing.

    `found` are detected doors (anything with `.location.page/x0/y0/x1/y1`).
    Where a part was widened and nothing read touches what was drawn, the door
    nearest the drawing is the answer -- a box drawn on a door's number but
    short of its swing still means that door.
    """
    def box_of(d) -> Box:
        loc = d.location
        return (loc.x0, loc.y0, loc.x1, loc.y1)

    kept = [d for d in found if answers(d.location.page, box_of(d), plans)]
    taken = {id(d) for d in kept}
    for p in plans:
        if p.whole or any(answers(d.location.page, box_of(d), [p]) for d in kept):
            continue
        r = p.read
        near = [d for d in found if id(d) not in taken and d.location.page == p.page
                and r[0] <= (d.location.x0 + d.location.x1) / 2 <= r[2]
                and r[1] <= (d.location.y0 + d.location.y1) / 2 <= r[3]]
        if not near:
            continue
        mid = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in p.drawn]

        def distance(d) -> float:
            b = box_of(d)
            cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            return min((cx - mx) ** 2 + (cy - my) ** 2 for mx, my in mid)

        best = min(near, key=distance)
        kept.append(best)
        taken.add(id(best))
    return kept
