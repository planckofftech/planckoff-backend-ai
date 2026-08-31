"""Pipeline 2 -- the schedule set against the drawings.

Pipeline 1 answers "what does the schedule say". This one answers "and does the
building agree", which is the question that costs money when nobody asks it.

Deliberately separate from `app.pipeline`: it reuses that pipeline's result
rather than re-reading the schedule, so the two can never disagree about what
the schedule contained. It also makes no AI call on this path at all -- every
door here is placed from text the drawing already carries.
"""

from __future__ import annotations

import logging
import math
import time
from contextvars import ContextVar
from pathlib import Path

from app.ai import pricing
from app.config import get_settings
from app.core import (
    table_locator,
    dimensions,
    door_match,
    door_reconcile,
    page_finder,
    plan_index,
    swing_finder,
)
from app.core.door_locator import DoorSighting, locate
from app.core.extractor import extract_pages
from app.core.pdf_doc import PdfDoc
from app.schemas import (
    DetectedDoorOut,
    DoorSwing,
    DoorLocation,
    DoorRow,
    DoorSightingOut,
    PlanAudit,
    ScanCost,
    SheetRef,
    UnscheduledDoorOut,
)

log = logging.getLogger(__name__)

# What the median door on a sheet measures, in points. Everything else is
# scaled off it from the schedule's own widths -- see app/core/dimensions.py.
_BASE_DOOR_PT = 36.0
# What share of a sheet's doors must be new before it is a different part of
# the building rather than the same rooms drawn again. A furniture plan repeats
# its floor plan almost exactly; a second storey repeats nothing.
_NEW_BUILDING_SHARE = 0.5
# How wide an opening must be, in leaf-lengths, before it can hold a pair.
# Halfway between one leaf and two, so slop in either measurement lands on
# the right side of it.
_PAIR_OPENING = 1.5
# How far a door's own swing may be from its printed number, in leaf
# lengths. Measured, and the drawings draw the line themselves. On a sheet
# that numbers its doors at the opening, eighteen of twenty tags sit
# 0.48-0.52 leaves from their own hinge and two more at 0.83-0.86. The next
# values are 1.52 and beyond, and those are the door next along. A sheet
# that numbers on leader lines has nothing nearer than 2.0.
#
# So one leaf sits in the gap: past it, the arc belongs to somebody else,
# and drawing this door on it is worse than leaving it unmeasured.
_TAG_REACH = 1.0
# What a caption has to say for a table to be the door schedule rather than
# one of the other schedules a drawing set carries.
_DOOR_WORDS = ("DOOR", "OPENING", "LEAF")
_DRY_RUN: ContextVar[bool] = ContextVar("plan_audit_dry_run", default=False)

_COVERAGE_NOTE = (
    "Doors are matched by their number as printed on the floor plans. This "
    "finds a door the schedule left out only if it is tagged on the drawing. "
    "A door drawn with no tag at all cannot be seen this way."
)
_DETECT_NOTE = (
    "Doors were found as shapes on the drawing, so a door with no number can "
    "be seen. Measured on one sheet with 36 known doors: 94% were found. The "
    "type comes from the model and is not verified against anything -- treat a "
    "door with no number as something to look at, not as a fact."
)


class NoDoorScheduleError(RuntimeError):
    def __init__(self, pages_scanned: int):
        self.pages_scanned = pages_scanned
        super().__init__(
            f"No door schedule found in this set - scanned {pages_scanned} pages. "
            "The audit compares the schedule against the plans, so it needs both."
        )


def _plan_per_level(plans, tags_on: dict[int, set[str]]):
    """The one sheet to lead with for each storey of the building.

    A reader asking for "the floor plan" means one thing per floor. A set gives
    them five sheets for a three-storey building: the two real floor plans, an
    OVERALL index that redraws both of them small, and the basement drawn twice
    over. Listing all five as peers is what made this view unreadable.

    So, per storey named in the titles:

      1. a plan of the whole floor beats an enlargement of part of it, because
         a corner of a building without its context cannot be checked
      2. then whichever carries the most of that floor's doors
      3. then the earlier sheet, so the choice does not move between runs

    Then the storeys are set against each other and any sheet whose doors are
    already covered elsewhere is dropped -- that is what removes a multi-floor
    OVERALL index, whose doors all belong to the per-floor sheets by definition.

    Returns the leading sheets and, separately, every other floor plan. The
    others are not discarded: doors are still measured on them, because an
    enlargement is usually where a swing can actually be read.
    """
    counted = [s for s in plans if tags_on.get(s.page)]
    if not counted:
        return [], list(plans)

    by_level: dict[str, list] = {}
    for sheet in counted:
        by_level.setdefault(sheet.level, []).append(sheet)

    leads = []
    for _level, sheets in by_level.items():
        sheets.sort(key=lambda s: (s.is_enlargement, -len(tags_on[s.page]),
                                   s.page))
        leads.append(sheets[0])
        # A storey is not always drawn on one sheet, and where a set names no
        # storey at all every sheet lands in one group. One project draws its
        # offices on one sheet and its warehouse across two more, all titled
        # without a level -- taking only the first would have hidden the
        # warehouse. So a second sheet in the same group leads too when its
        # doors are mostly its own: that is a different part of the building,
        # not another drawing of this one.
        held = set(tags_on[sheets[0].page])
        for sheet in sheets[1:]:
            tags = tags_on[sheet.page]
            if len(tags - held) < len(tags) * _NEW_BUILDING_SHARE:
                continue
            leads.append(sheet)
            held |= tags

    # A sheet whose doors are all somebody else's is an index of the others, not
    # a floor in its own right. Biggest first, so the real plans are chosen
    # before the reduced-scale copy gets a chance to represent them.
    leads.sort(key=lambda s: (-len(tags_on[s.page]), s.page))
    chosen, covered = [], set()
    for sheet in leads:
        tags = tags_on[sheet.page]
        if len(tags - covered) < len(tags) * _NEW_BUILDING_SHARE:
            continue
        chosen.append(sheet)
        covered |= tags

    chosen.sort(key=lambda s: s.page)
    lead_pages = {s.page for s in chosen}
    log.info("plan_audit leads with %s, one per level of %s",
             [f"{s.number} ({s.level or 'the building'})" for s in chosen],
             sorted({s.level for s in counted}))
    return chosen, [s for s in plans if s.page not in lead_pages]


def _sheets_worth_scanning(plans, tags_on: dict[int, set[str]]):
    """The sheets that are each a different part of the building.

    A set draws the same rooms over and over: an overall floor plan, then
    enlargements of its halves, then the same floor again for finishes. The
    same door is on all of them, and scanning all of them pays several times
    for one answer -- on one project 97 tiles where 12 covered every door,
    $3.78 against $0.47.

    Plain set-cover fixed the gross waste and left a subtler version of it. On
    a 37-door set the floor plan carried 36 and the furniture plan was then
    scanned in full to reach the one door left over: eight tiles for one door,
    half the bill, and 22 duplicate answers that contradicted the floor plan
    about what kind of door each was.

    So the test is not "does this sheet add a door" but "is this sheet a
    different part of the building". A redraw shares nearly all its doors with
    a sheet already chosen; another floor shares almost none:

        A8.1 FINISH & FURNITURE PLAN   35 of 36 doors also on A2.1  -> redraw
        A-102 SECOND FLOOR PLAN         0 of 24 doors also on A-101 -> scan it

    Whatever is skipped is named to the caller, never dropped quietly.
    """
    by_page = {s.page: s for s in plans}
    pages = [p for p in tags_on if p in by_page and tags_on[p]]
    if not pages:
        return [], [s.number or f"p{s.page}" for s in plans]

    # The sheet showing the most doors is the main plan by definition.
    pages.sort(key=lambda p: (-len(tags_on[p]), p))
    chosen = [by_page[pages[0]]]
    covered = set(tags_on[pages[0]])

    for page in pages[1:]:
        tags = tags_on[page]
        fresh = tags - covered
        if len(fresh) < len(tags) * _NEW_BUILDING_SHARE:
            continue
        chosen.append(by_page[page])
        covered |= tags

    # A door on none of the chosen sheets is not a redraw -- it is a door with
    # nowhere to be measured. This happens where a set prints a few doors only
    # on a sheet the overlap rule rejected: one project's exit plans carry six
    # doors (150, 160, 172, 174, 175, 177) that appear on no other drawing,
    # while sharing enough of their other doors to read as copies.
    #
    # So having decided which sheets are worth reading whole, add whatever else
    # is needed to reach the stragglers -- fewest sheets first, so one sheet
    # holding five leftovers is preferred to five sheets holding one each.
    leftover = {t for tags in tags_on.values() for t in tags} - covered
    while leftover:
        page = max(pages, key=lambda p: (len(tags_on[p] & leftover), -p))
        gained = tags_on[page] & leftover
        if not gained:
            break
        chosen.append(by_page[page])
        covered |= tags_on[page]
        leftover -= gained

    chosen.sort(key=lambda s: s.page)
    picked = {s.page for s in chosen}
    skipped = [s.number or f"p{s.page}" for s in plans if s.page not in picked]
    log.info("plan_scan will read %s and skip %s",
             [s.number for s in chosen] or "nothing", skipped or "nothing")
    return chosen, skipped


def _read_schedule(doc: PdfDoc, sheets, pages_scanned: int):
    """The door schedule, going straight to its sheet when the set names one.

    The sheet list already says `A7.10 - DOOR SCHEDULE`. Scoring all 187 pages
    to rediscover that costs twelve seconds and can still land on the hardware
    matrix printed beside it. So try the named sheets first, and only fall back
    to searching when the set does not name one -- which happens: one project
    prints its schedule on the floor plan sheet, with no title of its own.
    """
    named = plan_index.schedule_sheets(sheets)
    if named:
        pages = [page_finder.score_page(doc.text_items(s.page - 1), s.page)
                 for s in named]
        found = [e for e in extract_pages(doc, pages) if e.rows]
        if found:
            return _every_door(found)
        log.info("plan_audit: the named schedule sheets held no readable "
                 "table; searching every page instead")

    candidates = page_finder.passing(page_finder.find_schedule_pages(doc))
    extractions = [e for e in extract_pages(doc, candidates) if e.rows]
    if not extractions:
        raise NoDoorScheduleError(pages_scanned)
    return _every_door(extractions)


def _schedule_table(doc: PdfDoc, page: int
                    ) -> dict[int, list[tuple[float, float, float, float]]]:
    """Every schedule table on this page, so none is mistaken for a drawing.

    All of them, not the strongest. A sheet that prints its schedule in two
    halves side by side needs both blanked out: blanking only the left one, the
    audit went straight on to "find" doors 118-126 stacked in a neat column
    inside the right one, and again called it agreement.

    Empty when a table cannot be measured -- better to search a page twice than
    to blank out part of a plan.
    """
    if not page:
        return {}
    try:
        items = doc.text_items(page - 1)
        rulings = doc.rulings(page - 1)
        bands = (page_finder.header_bands(items, page)
                 or [page_finder.score_page(items, page)])
    except Exception:  # noqa: BLE001 - a page we cannot read is not fatal
        return {}

    boxes: list[tuple[float, float, float, float]] = []
    for band in bands:
        try:
            grid, _headers = table_locator.locate_table(
                items, rulings, band.header_y, band.tag_x)
        except Exception:  # noqa: BLE001 - one unreadable table is not fatal
            continue
        # The table ends at its last ruled row. Taking instead the lowest text
        # that happens to fall inside one of its columns reaches all the way
        # down the sheet -- the plan sits under the table and shares its x
        # range -- and blanks out the very drawing we came to search.
        if grid.row_bounds:
            bottom = grid.row_bounds[-1]
        else:
            inside = [i.y1 for i in items
                      if i.horizontal and i.cy > grid.header_bottom
                      and grid.column_of(i.x0) is not None]
            bottom = max(inside, default=grid.header_bottom)
        boxes.append((grid.left, grid.header_top, grid.right, bottom))

    if not boxes:
        log.info("plan_audit could not measure the schedule on page %d; its "
                 "own door numbers may be read as doors on the drawing", page)
        return {}
    log.info("plan_audit will ignore %d schedule table(s) on page %d: %s",
             len(boxes), page,
             ["x %.0f-%.0f y %.0f-%.0f" % (b[0], b[2], b[1], b[3])
              for b in boxes])
    return {page: boxes}


def _every_door(found: list) -> tuple[list[DoorRow], int]:
    """All the doors the sheet schedules, not the biggest table's doors.

    One sheet can carry a schedule in two halves printed side by side -- doors
    101-117 on the left, 118-126 on the right. Auditing the winner alone left
    ten of that project's twenty-five doors never looked for on the drawings,
    and nothing said so.

    Every table whose caption says it is about doors counts, whatever its size.
    Keeping only those tying the best score is not the same thing and was wrong
    for exactly the case this exists for: the two halves of one schedule differ
    in row count, so the smaller half scored lower and was dropped again.

    When nothing says "door" -- a schedule printed with no caption at all --
    the single most door-like table is read rather than everything on the page,
    because at that point there is nothing to tell a door schedule apart from
    the window schedule beside it.
    """
    said_so = [e for e in found
               if any(w in (e.title or "").upper() for w in _DOOR_WORDS)]
    doors = said_so or [max(found, key=_door_schedule_score)]

    rows: list[DoorRow] = []
    seen: set[str] = set()
    for extraction in doors:
        for row in extraction.rows:
            if row.door_tag and row.door_tag in seen:
                continue
            if row.door_tag:
                seen.add(row.door_tag)
            rows.append(row)

    page = doors[0].page
    log.info("plan_audit read %d schedule(s) on page %d: %d doors",
             len(doors), page, len(rows))
    return rows, page


def _door_schedule_score(extraction) -> tuple[int, int, int]:
    """How much this table looks like *the* door schedule.

    Taking the biggest table was the obvious rule and it is wrong. A drawing set
    carries schedules for everything -- one 164-page set briefly produced a
    107-row table of heat pumps (`B-HP-1`, `B-HP-2`...) beside its 40-row door
    schedule, and the heat pumps won on size alone.

    So: a table whose caption says DOOR outranks any number of rows, then how
    much of it we could actually understand as door columns, and only then size.
    """
    title = (extraction.title or "").upper()
    says_door = any(word in title for word in _DOOR_WORDS)
    fields = len({f for f in extraction.mapped if f})
    return (1 if says_door else 0, fields, len(extraction.rows))


def _fraction(location, width: float, height: float) -> DoorLocation:
    return DoorLocation(
        page=location.page, sheet=location.sheet,
        x0=location.x0 / width, y0=location.y0 / height,
        x1=location.x1 / width, y1=location.y1 / height,
    )


def _out(sighting: DoorSighting, sizes: dict[int, tuple[float, float]]) -> DoorSightingOut:
    return DoorSightingOut(
        tag=sighting.tag,
        confidence=sighting.confidence,
        locations=[
            _fraction(c, *sizes[c.page]) for c in sighting.candidates
            if c.page in sizes
        ],
    )


def _kind(leaves: int, width_ft: float | None, per_foot: float | None,
          radius: float) -> str:
    """Single or pair, from the drawing and the schedule together.

    A pair hangs two leaves in one opening, so the opening is two leaves wide.
    The drawing gives the leaf -- the arc's radius -- and the schedule gives
    the opening. When they agree the answer is certain, and when the schedule
    says three feet no arrangement of arcs makes it a pair.

    Geometry alone could not do it. On one sheet it called 102B and 136B pairs
    -- both 3'-0" singles -- and missed 114, the only 6'-0" opening on the
    plan. What fooled it was the next door along standing about one leaf away,
    which is a distance nothing in a single arc can distinguish from a jamb.
    """
    if width_ft and per_foot:
        opening = width_ft * per_foot
        return "double_swing" if opening >= radius * _PAIR_OPENING else "single_swing"
    # No width to check against: fall back to what the arcs suggest.
    return "double_swing" if leaves >= 2 else "single_swing"


def _measure_from_tags(doc: PdfDoc, plans, sightings, rows,
                       sizes) -> list[DetectedDoorOut]:
    """Measure each door's swing starting from its printed number. No AI.

    The detector exists to find doors nobody numbered. But every door that IS
    numbered has already been found, for nothing, by `door_locator` -- and a
    door tag is printed at its door. So the tag is as good a starting point as
    a model's guess, and it is free and exact.

    That gives the same measured swing, leaf length and box the paid pass
    produces, on every numbered door, at no cost: on one sheet 27 of its 36
    doors come back with a fitted arc this way.

    What it cannot do is see a door with no number on it. That is the one thing
    the model is for, and why this does not replace it.
    """
    widths = [r.door_width for r in rows if r.door_width]
    median_ft = dimensions.median_width_ft(widths)
    by_tag = {
        r.door_tag: {k: v for k, v in
                     {**r.model_dump(exclude={"extra"}), **r.extra}.items()
                     if isinstance(v, str) and v and k != "door_tag"}
        for r in rows if r.door_tag
    }

    seeds: dict[int, list[tuple[str, float, float]]] = {}
    for sighting in sightings:
        for c in sighting.candidates:
            width, height = sizes.get(c.page, (0.0, 0.0))
            if width and height:
                seeds.setdefault(c.page, []).append(
                    (sighting.tag, (c.x0 + c.x1) / 2, (c.y0 + c.y1) / 2))

    # Every sheet, not a chosen few. Reading only the sheet with the most doors
    # on it costs the arcs: that sheet is the overall plan, where a door is a
    # few points across and its swing is drawn over by dimensions and hatching.
    # The enlargement of the same rooms is the drawing that can actually be
    # measured -- on one project the two enlarged sheets held 11 of the 13
    # swings found, and selecting sheets by door count threw both away.
    #
    # So measure the door wherever it is drawn, and decide afterwards which
    # drawing to believe. `_group_by_door` does that.
    sheet_of = {s.page: (s.number or f"p{s.page}") for s in plans}
    scale_of: dict[int, float] = {}

    out: list[DetectedDoorOut] = []
    for page, found in seeds.items():
        if page not in sheet_of:
            continue
        width, height = sizes[page]
        points = [(x, y) for _tag, x, y in found]
        radius = swing_finder.calibrate(doc, page, points)
        door_pt = radius if radius else _BASE_DOOR_PT
        # The calibrated leaf length *is* the scale, measured off the drawing.
        # Zero where calibration found nothing, which ranks the sheet last
        # rather than pretending the fallback figure was measured.
        scale_of[page] = radius or 0.0
        per_foot = (radius / median_ft) if (radius and median_ft) else None
        # Whose arc is it, not how near is it -- see arcs_for_tags. A fixed
        # reach cannot serve a sheet that numbers at the opening and one that
        # numbers on leader lines, and this project has both.
        arcs = swing_finder.arcs_for_tags(doc, page, points, expected_r=radius,
                                          door_pt=door_pt)

        for (tag, x, y), arc in zip(found, arcs):
            half = door_pt / 2
            entry = DetectedDoorOut(
                location=DoorLocation(
                    page=page, sheet=sheet_of[page],
                    x0=(x - half) / width, y0=(y - half) / height,
                    x1=(x + half) / width, y1=(y + half) / height),
                type="unknown", tag=tag, schedule=by_tag.get(tag, {}),
                confidence="unique",
            )
            # No fixed cap here any more. arcs_for_tags refuses an arc whose
            # own nearest number is somebody else, which is the real test --
            # and it lets a leadered number reach as far as it needs to.
            if arc is not None:
                entry.location = DoorLocation(
                    page=page, sheet=sheet_of[page],
                    x0=arc.x0 / width, y0=arc.y0 / height,
                    x1=arc.x1 / width, y1=arc.y1 / height)
                entry.source = "geometry"
                entry.arc = DoorSwing(
                    hinge_x=arc.hinge_x, hinge_y=arc.hinge_y,
                    radius=arc.radius, start_deg=arc.start_deg,
                    end_deg=arc.end_deg, residual=arc.residual)
                leaves = swing_finder.leaves_at(doc, page, x, y,
                                                expected_r=radius,
                                                door_pt=door_pt)
                entry.type = _kind(
                    leaves, dimensions.parse_feet(
                        (by_tag.get(tag) or {}).get("door_width", "")),
                    per_foot, radius or arc.radius)
                if entry.type == "double_swing":
                    mate = swing_finder.other_leaf(doc, page, arc, radius,
                                                   door_pt)
                    if mate:
                        entry.other_leaf = DoorSwing(
                            hinge_x=mate.hinge_x, hinge_y=mate.hinge_y,
                            radius=mate.radius, start_deg=mate.start_deg,
                            end_deg=mate.end_deg, residual=mate.residual)
                        entry.location = DoorLocation(
                            page=page, sheet=sheet_of[page],
                            x0=min(arc.x0, mate.x0) / width,
                            y0=min(arc.y0, mate.y0) / height,
                            x1=max(arc.x1, mate.x1) / width,
                            y1=max(arc.y1, mate.y1) / height)
                if per_foot:
                    entry.measured_width = dimensions.feet_inches(
                        arc.radius / per_foot)
            entry.sheet_scale = round(scale_of[page], 2)
            out.append(entry)

    _group_by_door(out)
    counted = [d for d in out if d.primary]
    log.info("plan_audit measured %d drawing(s) of %d door(s), %d with a "
             "fitted swing", len(out), len(counted),
             sum(1 for d in counted if d.arc))
    return out


def _group_by_door(doors: list[DetectedDoorOut]) -> None:
    """One door, one line -- whichever sheet drew it best.

    The same door is drawn on several sheets and each drawing was measured, so
    an 85-door project produced 144 rows. Deleting the extras is the wrong fix:
    the viewer needs them to draw the door on whatever sheet is open, and the
    sheet with the most doors on it is rarely the sheet that measured them
    best. So keep every drawing and elect one of them.

    The election, in order:

      1. a drawing with a fitted swing beats one without -- a measurement beats
         an assumption, whatever sheet either came from
      2. then the larger scale, because a door drawn twice the size was read
         with twice the certainty
      3. then the earlier sheet, so the answer does not move between runs

    Marks the winner `primary` and gives every drawing of that door the same
    `also_on` list, smallest scale first: overall plan, partial, enlargement.
    """
    by_tag: dict[str, list[DetectedDoorOut]] = {}
    for door in doors:
        if door.tag:
            by_tag.setdefault(door.tag, []).append(door)

    for drawings in by_tag.values():
        best = max(drawings, key=lambda d: (d.arc is not None, d.sheet_scale,
                                            -d.location.page))
        # Smallest scale first is the order a set is read in: the overall plan
        # tells you where the door is, the enlargement tells you what it is.
        chain = [d.location.sheet for d in
                 sorted(drawings, key=lambda d: (d.sheet_scale,
                                                 d.location.page))]
        for door in drawings:
            door.primary = door is best
            door.also_on = chain


async def _detect(doc: PdfDoc, plans, sightings, rows, sizes,
                  budget_usd: float, names: dict[int, str]
                  ) -> tuple[list[DetectedDoorOut], ScanCost, list[str]]:
    """Find the doors as shapes, and match each to the schedule door beside it.

    Measured on one sheet with 36 known doors: 94% of them found, and the boxes
    land on the doors rather than on their number labels. Two things earned
    that, and both are worth keeping when this is tuned again:

      zoom   a door must be about 160 px in the picture the model sees. At 80 px
             recall was 78% and the boxes landed in the middle of open rooms.
      point  ask where the door *is*, not for a rectangle around it. Asked for a
             rectangle, one model returned a lattice of identical boxes stepping
             down a column, every one "high confidence".

    The box's size never comes from the model, which has no way to know it. It
    comes from the schedule -- so a pair written "(2)3' - 0"" is drawn twice the
    width of a single.
    """
    from app.ai.plan_scan import scan_sheet

    settings = get_settings()
    widths = [r.door_width for r in rows if r.door_width]
    median_ft = dimensions.median_width_ft(widths)
    spans = {
        r.door_tag: dimensions.door_span_pt(r.door_width, median_ft, _BASE_DOOR_PT)
        for r in rows if r.door_tag
    }
    # The schedule row for each door, so the panel that opens when one is
    # clicked can show what the schedule says without a second request.
    by_tag = {
        r.door_tag: {k: v for k, v in
                     {**r.model_dump(exclude={"extra"}), **r.extra}.items()
                     if isinstance(v, str) and v and k != "door_tag"}
        for r in rows if r.door_tag
    }

    by_page: dict[int, list[tuple[float, float]]] = {}
    tags_on: dict[int, set[str]] = {}
    for sighting in sightings:
        for candidate in sighting.candidates:
            width, height = sizes.get(candidate.page, (0.0, 0.0))
            if width and height:
                by_page.setdefault(candidate.page, []).append(
                    ((candidate.x0 + candidate.x1) / 2 / width,
                     (candidate.y0 + candidate.y1) / 2 / height))
                tags_on.setdefault(candidate.page, set()).add(sighting.tag)

    cost = ScanCost(model=settings.ai_detect, dry_run=_DRY_RUN.get(False))
    found: list[DetectedDoorOut] = []
    warnings: list[str] = []
    cache: dict[str, list[dict]] = {}

    chosen, skipped = _sheets_worth_scanning(plans, tags_on)
    scanned_tags = {t for s in chosen for t in tags_on.get(s.page, set())}
    if skipped:
        warnings.append(
            "Skipped " + ", ".join(skipped) + ": "
            + ("that sheet draws" if len(skipped) == 1 else "those sheets draw")
            + " the same rooms as a sheet being scanned, so every door on "
            + ("it is" if len(skipped) == 1 else "them is")
            + " already accounted for."
        )
    # Named, never dropped quietly: these doors are on the drawings and were
    # found there by number, but the sheet they are on is not being looked at,
    # so no shape will be detected for them.
    elsewhere = sorted({t for tags in tags_on.values() for t in tags}
                       - scanned_tags)
    if elsewhere:
        where = {
            tag: "/".join(sorted({names.get(page, f"p{page}")
                                  for page, tags in tags_on.items()
                                  if tag in tags}))
            for tag in elsewhere
        }
        warnings.append(
            "Not looked for as a shape: " + ", ".join(
                f"{tag} (on {where[tag]})" for tag in elsewhere)
            + " -- printed only on a sheet that is not being scanned."
        )

    for sheet in chosen:
        # The ceiling is for the whole request, not for one sheet. The previous
        # per-sheet cap never fired: a five-sheet project could spend five
        # times it and stay silent, which is how two projects cost $6 against a
        # stated $0.70.
        width, height = doc.page_size(sheet.page - 1)
        known = by_page.get(sheet.page, [])

        # Measure the sheet before photographing it. The zoom is chosen to put
        # a door near 160 px, and that needs to know how big a door is *here*:
        # assuming 36 pt everywhere put an AT&T door -- drawn at 1/16" scale,
        # 27 pt wide -- at about 120 px, in the range where the model starts
        # missing them. This costs nothing; it reads arcs already in the file.
        radius = swing_finder.calibrate(
            doc, sheet.page, [(x * width, y * height) for x, y in known])
        # The fitted radius IS the door's width: a swing arc is struck from the
        # hinge with the leaf as the radius, so the leaf's length is the radius.
        # Doubling it here told the renderer every door was twice its real size,
        # which put all six projects at about 80 px a door instead of 160 -- the
        # difference between finding 78% of them and 94%.
        door_pt = radius if radius else _BASE_DOOR_PT
        per_foot = (radius / median_ft) if (radius and median_ft) else None
        if radius:
            log.info("plan_scan page %s: doors measure %.0f pt across, so the "
                     "sheet is rendered for that rather than the assumed %.0f",
                     sheet.page, door_pt, _BASE_DOOR_PT)

        report = await scan_sheet(doc, sheet.page, known,
                                  door_pt=door_pt,
                                  spans=spans, dry_run=cost.dry_run, cache=cache,
                                  budget_usd=budget_usd,
                                  spent_usd=cost.estimated_usd)
        cost.sheets += 1
        cost.predicted_usd += report.predicted_usd
        cost.tiles_planned += report.tiles_planned
        cost.tiles_sent += report.tiles_sent
        cost.prompt_tokens += report.prompt_tokens
        cost.completion_tokens += report.completion_tokens
        warnings.extend(report.warnings)

        # Measure every shape first, and only then decide which door it is.
        #
        # The other way round -- label from the model's approximate point, then
        # move the box onto the arc -- means the number and the rectangle can
        # end up describing two different doors, which is exactly what showed
        # up on screen: a box drawn on one door carrying its neighbour's
        # number. Nothing is named here until its box has stopped moving.
        # Every shape's swing settled together, so no two of them are handed
        # the same arc -- see swing_finder.assign_swings. Doing it one at a
        # time let a door's swing be taken by its neighbour, and twelve real
        # doors on one sheet were then reported as not drawn.
        rank = {"high": 3, "medium": 2, "low": 1, "": 0}
        seen = sorted(report.doors, key=lambda d: -rank.get(d.confidence, 0))
        arcs = swing_finder.assign_swings(
            doc, sheet.page,
            [(d.cx * width, d.cy * height) for d in seen],
            expected_r=radius)

        on_sheet: list[DetectedDoorOut] = []
        read: list[str] = []  # what the model said each one's number was

        for door, arc in zip(seen, arcs):
            entry = DetectedDoorOut(
                location=DoorLocation(page=door.page, sheet=sheet.number,
                                      x0=door.x0, y0=door.y0,
                                      x1=door.x1, y1=door.y1),
                type=door.type, swing=door.swing, confidence=door.confidence,
            )
            if arc is not None:
                # The drawing wins over the model, every time. This box is the
                # measured extent of the arc and its leaf, not a rectangle
                # guessed from an assumed size around an approximate point.
                entry.location = DoorLocation(
                    page=door.page, sheet=sheet.number,
                    x0=arc.x0 / width, y0=arc.y0 / height,
                    x1=arc.x1 / width, y1=arc.y1 / height)
                entry.source = "geometry"
                # An arc means it swings, whatever the model called it. This
                # is not a preference, it is the drawing: a pocket door slides
                # into the wall and a cased opening has no leaf, so neither can
                # have a quarter-circle struck from a hinge.
                #
                # Measured on one sheet: 10 of 33 numbered doors came back
                # "pocket", "sliding" or "opening, no door" while carrying a
                # fitted arc at 26.9-27.4 pt against the sheet's own door
                # radius of 27.0. Every one of them swings.
                #
                # How many leaves turn about the opening then says single or
                # pair -- again counted off the ink, not guessed.
                leaves = swing_finder.leaves_at(
                    doc, sheet.page, door.cx * width, door.cy * height,
                    expected_r=radius, door_pt=door_pt)
                was = entry.type
                entry.type = _kind(
                    leaves, dimensions.parse_feet(
                        (by_tag.get(door.tag.strip()) or {}).get(
                            "door_width", "") if door.tag else ""),
                    per_foot, radius or arc.radius)
                if was != entry.type:
                    log.info("plan_scan page %s: door %r called %r by the "
                             "model, but the drawing has %d swing arc(s) -- "
                             "recorded as %r", sheet.page, door.tag or "?",
                             was, leaves, entry.type)
                # The measurement itself, not just the box it implies. A box
                # says "a door is about here"; this says which way it opens and
                # how far, which is what makes it drawable as a door.
                entry.arc = DoorSwing(
                    hinge_x=arc.hinge_x, hinge_y=arc.hinge_y,
                    radius=arc.radius, start_deg=arc.start_deg,
                    end_deg=arc.end_deg, residual=arc.residual)
                if per_foot:
                    entry.measured_width = dimensions.feet_inches(arc.radius / per_foot)
            else:
                # No arc. That is not the same as no door: a pocket door, a
                # slider, a barn door and a roll-up shutter are all drawn as a
                # panel with no arc at all. One warehouse schedules two thirds
                # of its doors as sectional overhead, and every one of them was
                # coming back as "nothing confirms this".
                #
                # So look for the panel, sized from that door's own scheduled
                # width -- an 8'-0" shutter is not the same length of line as a
                # 3'-0" door, and a sheet carries both.
                span = spans.get(door.tag.strip()) if door.tag else None
                leaf_pt = (span * door_pt / _BASE_DOOR_PT) if span else door_pt
                leaf = swing_finder.find_leaf(
                    doc, sheet.page, door.cx * width, door.cy * height, leaf_pt)
                if leaf is not None:
                    entry.location = DoorLocation(
                        page=door.page, sheet=sheet.number,
                        x0=min(leaf.x0, leaf.x1) / width,
                        y0=min(leaf.y0, leaf.y1) / height,
                        x1=max(leaf.x0, leaf.x1) / width,
                        y1=max(leaf.y0, leaf.y1) / height)
                    entry.source = "geometry"
                    if per_foot:
                        entry.measured_width = dimensions.feet_inches(
                            leaf.length / per_foot)
                elif swing_finder.swings(door.type):
                    # It was called a swinging door, and the drawing has
                    # neither a swing nor a panel. Asserting a door here would
                    # be inventing one.
                    continue
            on_sheet.append(entry)
            read.append(door.tag)

        _name_doors(on_sheet, read, sightings, sheet.page, width, height, by_tag)

        # A shape with no measured arc AND no schedule number is the model's
        # word and nothing else. Neither the drawing nor the schedule says a
        # door is there, and there is no way to check it.
        #
        # Only the types that have no arc to find can reach this point --
        # openings, pockets, sliders -- so the geometry gate never applied to
        # them and they were drawn on the sheet as confidently as a measured
        # door. On one real plan that put red boxes labelled "? opening" on
        # walls and dimension lines and a "? pocket" on a structural column.
        #
        # A door that is not there is worse than a door we missed: it gets
        # priced, and the estimator stops trusting the whole report. So these
        # are counted and named, not shown.
        confirmed, unverified = _confirmed(on_sheet)
        if unverified:
            kinds = sorted({d.type or "unknown" for d in unverified})
            warnings.append(
                f"{len(unverified)} shape(s) on {sheet.number} were called a "
                f"door ({', '.join(kinds)}) but carry no schedule number and "
                "have no swing arc to measure, so nothing confirms them. "
                "Not shown."
            )
        found.extend(confirmed)

    found = _one_per_door(found)

    cost.estimated_usd = round(
        pricing.cost_usd(cost.model, cost.prompt_tokens, cost.completion_tokens), 4)
    cost.predicted_usd = round(cost.predicted_usd, 4)
    if cost.tiles_sent < cost.tiles_planned and not cost.dry_run:
        warnings.append(
            f"Only {cost.tiles_sent} of {cost.tiles_planned} tiles were read "
            f"before the ${budget_usd:.2f} ceiling. Doors on the parts not "
            f"read will be missing. Raise DETECT_BUDGET_USD to scan it all."
        )
    return found, cost, warnings


def _name_doors(doors: list[DetectedDoorOut], read: list[str], sightings,
                page: int, width: float, height: float,
                by_tag: dict[str, dict]) -> None:
    """Put a schedule number on each shape, in one decision for the sheet.

    Modifies the entries in place. See `app/core/door_match.py` for why this
    cannot be done one door at a time.
    """
    tags = {
        sighting.tag: door_match.Spot(c.x0, c.y0, c.x1, c.y1)
        for sighting in sightings
        for c in sighting.candidates if c.page == page
    }
    boxes = [
        door_match.Spot(d.location.x0 * width, d.location.y0 * height,
                        d.location.x1 * width, d.location.y1 * height)
        for d in doors
    ]
    for index, tag in door_match.assign(boxes, read, tags).items():
        doors[index].tag = tag
        doors[index].schedule = by_tag.get(tag, {})


def _confirmed(doors: list[DetectedDoorOut]
               ) -> tuple[list[DetectedDoorOut], list[DetectedDoorOut]]:
    """Split off the shapes nothing corroborates. Returns (shown, withheld).

    A detection earns its place two ways: the drawing agrees -- an arc was
    fitted to real ink and the box is its measured extent -- or the schedule
    agrees, because a scheduled door number is printed beside it. A shape with
    neither is the model's word alone.

    Only the types that have no arc to find can end up in that position:
    openings, pockets, sliders. The swing test never applied to them, so they
    were drawn on the sheet as confidently as a measured door -- and on one
    real plan that meant red boxes reading "? opening" on walls and dimension
    lines, and a "? pocket" on a structural steel column.

    A door that is not there is worse than one that was missed. It gets priced,
    and it is the reason the whole report stops being believed. So these are
    counted and named to the caller, and left off the drawing.
    """
    shown, withheld = [], []
    for door in doors:
        # A schedule number is corroboration enough on its own: the schedule
        # says the door exists and its number is printed right there.
        #
        # With no number, the claim is "here is a door nobody scheduled" --
        # the most expensive thing this service says, because somebody prices
        # it. That needs the strongest evidence available, which is a fitted
        # arc. A found leaf is not enough: a leaf is a straight line about as
        # long as a door, and a busy plan is full of those. Measured on one
        # sheet, accepting a leaf turned 6 unscheduled finds into 38, and 32 of
        # the 38 rested on a line alone.
        vouched = door.tag or door.arc is not None
        (shown if vouched else withheld).append(door)
    return shown, withheld


def _one_per_door(doors: list[DetectedDoorOut]) -> list[DetectedDoorOut]:
    """Collapse detections that turned out to be the same door.

    Runs once over every sheet together, not once per sheet. Per sheet it could
    not see the duplicate that matters most: a building drawn twice puts the
    same door on two sheets, and one 37-door set came back as 53 rows whose two
    halves disagreed about what kind of door several of them were.

    A door number is the identity here -- two detections resolving to the same
    number ARE one door, whatever the geometry says, and no distance threshold
    can tell "the same door twice" from "a pair of doors side by side" because
    those are the same distance apart. Detections with no number keep the
    distance test, since there is nothing else to go on.
    """
    rank = {"high": 3, "medium": 2, "low": 1, "": 0}
    best: dict[str, DetectedDoorOut] = {}
    loose: list[DetectedDoorOut] = []

    # A measured box outranks a guessed one, then the model's own confidence.
    # Numbered doors are settled first, so an unnumbered shape is judged
    # against the doors we are sure of rather than against whatever happened
    # to come first.
    ordered = sorted(doors, key=lambda d: (not d.tag, d.source != "geometry",
                                           -rank.get(d.confidence, 0)))
    for door in ordered:
        if door.tag:
            best.setdefault(door.tag, door)
            continue
        # An unnumbered shape sitting on top of a door we have already placed
        # is that same door looked at twice, not a door nobody scheduled. Two
        # doors cannot occupy one piece of floor, so overlap says it outright
        # -- no threshold to pick, and none to be wrong about. Measured on one
        # sheet: three of four unnumbered shapes overlapped a numbered door
        # (by 2, 13 and 27 pt), and the fourth stood 37 pt clear of everything.
        if not any(_overlaps(door.location, o.location)
                   for o in [*best.values(), *loose]):
            loose.append(door)

    return [*best.values(), *loose]


def _overlaps(a: DoorLocation, b: DoorLocation) -> bool:
    """Do these two boxes cover any of the same drawing?"""
    return (a.page == b.page
            and a.x0 < b.x1 and b.x0 < a.x1
            and a.y0 < b.y1 and b.y0 < a.y1)


async def audit(source: bytes | str | Path, *, detect: bool = False,
                dry_run: bool = False,
                budget_usd: float | None = None,
                rows: list[DoorRow] | None = None,
                schedule_page: int = 0) -> PlanAudit:
    """PDF in, reconciliation out. Stateless.

    `rows` is a schedule that has already been read. Pass it and this does not
    read one: the search and the extraction are skipped entirely.

    That matters for three reasons. It halves the work -- the caller has just
    extracted this schedule and re-reading a 438 MB set costs another 40
    seconds. It removes a way for the two to disagree, since the audit is now
    reconciling exactly the rows on the caller's screen. And it is the only way
    to audit a set whose schedule lives in a *different* file, which is real:
    one project ships `Drawings.pdf` and `Takeoff/Door Schedule.pdf` separately.

    `detect` turns on the vision pass that finds doors as shapes. It is off by
    default because it is the only part of this that costs money; `dry_run`
    reports what it would send and sends nothing.
    """
    started = time.perf_counter()
    _DRY_RUN.set(dry_run)

    with PdfDoc(source) as doc:
        pages_scanned = doc.page_count

        sheets = plan_index.index_sheets(doc)

        if rows:
            best_page = schedule_page
            log.info("plan_audit using %d rows supplied by the caller; "
                     "not re-reading the schedule", len(rows))
        else:
            rows, best_page = _read_schedule(doc, sheets, pages_scanned)

        tags = {r.door_tag for r in rows if r.door_tag}
        plans = plan_index.floor_plans(sheets)

        # A small project prints its schedule on the floor plan sheet, and the
        # schedule's own column of door numbers is then the easiest thing on
        # that page to find. Blank it out, or the audit compares the schedule
        # with itself and reports that everything agrees.
        avoid = _schedule_table(doc, best_page)

        sightings = locate(doc, tags, plans, avoid)

        # Second pass, for the doors the plainly-titled plans did not account
        # for. Sheet titles are not dependable enough to be the only filter --
        # see plan_index.other_architectural.
        missing = {s.tag for s in sightings if not s.found}
        searched = list(plans)
        if missing:
            spare = plan_index.other_architectural(sheets, plans)
            if spare:
                log.info("plan_audit widening to %d more architectural sheets "
                         "for %d unplaced doors", len(spare), len(missing))
                extra = {s.tag: s for s in locate(doc, missing, spare, avoid)}
                sightings = [extra.get(s.tag, s) if not s.found else s
                             for s in sightings]
                searched += spare

        unscheduled = door_reconcile.tagged_but_unscheduled(doc, tags, plans)
        result = door_reconcile.reconcile(rows, sightings, plans, unscheduled)

        # Page sizes are needed to express positions as fractions of the page,
        # which is what the preview draws with.
        pages = {c.page for s in sightings for c in s.candidates}
        pages |= {u.page for u in unscheduled}
        # Every floor plan too, whether or not a door was placed on it: a
        # viewer that draws arcs over the sheet needs its size to set up the
        # coordinate system, and a plan with nothing found on it is exactly the
        # one somebody will want to look at.
        pages |= {s.page for s in searched}
        sizes = {p: doc.page_size(p - 1) for p in pages}

        detected: list[DetectedDoorOut] = []
        scan_cost = None
        if detect:
            ceiling = (budget_usd if budget_usd is not None
                       else get_settings().detect_budget_usd)
            detected, scan_cost, scan_warnings = await _detect(
                doc, plans, sightings, rows, sizes, ceiling,
                {s.page: s.number or f"p{s.page}" for s in sheets})
            result.warnings.extend(scan_warnings)
            unlabelled = [d for d in detected if not d.tag]
            if unlabelled:
                result.warnings.append(
                    f"{len(unlabelled)} door(s) were found on the drawings with "
                    "no schedule number beside them; check each against its crop "
                    "before pricing it"
                )
        else:
            # No model, no bill. Every numbered door has already been placed
            # from the drawing's own text, and a door tag is printed at its
            # door -- so the tag seeds the same measurement the paid pass makes.
            # Floor plans only, never the sheets the widened search added. That
            # second pass exists to *locate* a door number when sheet titles are
            # unreliable, and on a code sheet it matches the number inside an
            # occupancy table -- "STER #013 / B / 175 / 1.18" is not door 175.
            # Measuring there drew boxes on calculation tables. A door printed
            # nowhere but a code sheet is reported as not found on any floor
            # plan, which is what it is.
            detected = _measure_from_tags(doc, plans, sightings, rows, sizes)

        # Which sheet leads for each storey. Worked out from where the door
        # numbers actually landed, so it reflects the drawings rather than the
        # titles alone.
        tags_on: dict[int, set[str]] = {}
        for sighting in sightings:
            for c in sighting.candidates:
                tags_on.setdefault(c.page, set()).add(sighting.tag)
        leads, _rest = _plan_per_level(plans, tags_on)
        lead_pages = {s.page for s in leads}

    duration = int((time.perf_counter() - started) * 1000)
    log.info("plan_audit pages=%s doors=%s located=%s detected=%s ms=%s",
             pages_scanned, len(tags), len(result.found), len(detected),
             duration)

    return PlanAudit(
        pages_scanned=pages_scanned,
        duration_ms=duration,
        schedule_page=best_page,
        floor_plans=[
            # `scanned` says whether any door number was actually found here.
            # A sheet can be a genuine floor plan and still yield nothing: an
            # overall plan at 1/16" redraws doors the partial plans already
            # carry at 1/8". Without this, a deliberate skip and a real miss
            # both reach the caller as a bare zero.
            SheetRef(page=s.page, number=s.number, title=s.title,
                     width=sizes.get(s.page, (0.0, 0.0))[0],
                     height=sizes.get(s.page, (0.0, 0.0))[1],
                     level=s.level, leads=s.page in lead_pages,
                     scanned=bool(tags_on.get(s.page)),
                     is_enlargement=s.is_enlargement)
            for s in plans
        ],
        door_count=len(tags),
        located=[_out(s, sizes) for s in result.found],
        not_on_plans=[_out(s, sizes) for s in result.missing_from_plans],
        unscheduled=[
            UnscheduledDoorOut(
                label=u.label, reasons=u.reasons,
                location=_fraction(u, *sizes[u.page]),
            )
            for u in result.unscheduled if u.page in sizes
        ],
        warnings=result.warnings,
        coverage_note=_COVERAGE_NOTE if not detect else _DETECT_NOTE,
        detected=detected,
        scan_cost=scan_cost,
    )
