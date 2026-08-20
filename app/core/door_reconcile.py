"""Set the schedule against the drawings and report where they disagree.

The schedule is a claim about the building; the plans are another. An estimator
is caught out by the gap between them -- a door drawn but never scheduled is a
door nobody prices, and a door scheduled but not drawn is a line item with
nothing behind it.

Three buckets, and the two that matter are the ones that are not "fine".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.core.door_locator import DoorSighting
from app.core.page_finder import TAG_RE
from app.core.pdf_doc import PdfDoc, TextItem
from app.core.plan_index import SHEET_NUMBER, PlanSheet
from app.schemas import DoorRow

log = logging.getLogger(__name__)

# Text that is tag-shaped but is plainly not a door: a reference to another
# sheet (A3.1, A4.51) or a finish/material code (PT-1, F3.3, CL-01).
_NOT_A_DOOR = re.compile(r"^[A-Z]{1,2}[-.]?\d", re.I)
# A candidate needs this much supporting company to be worth reporting; the
# same scoring the locator uses.
_MIN_UNSCHEDULED_SCORE = 2
# A numbering strip is at least this long, on one baseline within this tolerance.
_MIN_NUMBER_RUN = 4
_RUN_BASELINE_TOL = 6.0


@dataclass(slots=True)
class UnscheduledDoor:
    label: str
    page: int
    sheet: str
    x0: float
    y0: float
    x1: float
    y1: float
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Reconciliation:
    floor_plans: list[PlanSheet] = field(default_factory=list)
    found: list[DoorSighting] = field(default_factory=list)
    missing_from_plans: list[DoorSighting] = field(default_factory=list)
    unscheduled: list[UnscheduledDoor] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return (f"{len(self.found)} of {len(self.found) + len(self.missing_from_plans)} "
                f"scheduled doors located; {len(self.unscheduled)} on the plans "
                f"but not scheduled")


def _is_not_a_door(text: str) -> bool:
    """Sheet references and finish codes are tag-shaped and everywhere.

    Without this, one floor plan reported 32 "unscheduled doors", every one of
    them a cross-reference like A4.51 or a paint code like PT-1.
    """
    return bool(SHEET_NUMBER.match(text) or _NOT_A_DOOR.match(text))


def _in_a_numbered_run(item: TextItem, items: list[TextItem]) -> bool:
    """Is this one of a strip of consecutive numbers -- parking bays, grid lines?

    A basement sheet numbers its parking bays 32, 33, 34, 35 along one line,
    evenly spaced. Those are door-tag shaped, sit beside each other, and one of
    them was reported as an unscheduled door.

    Doors do run in sequence along a corridor, so the bar is deliberately high:
    four or more, all on one baseline, each step changing the number by one.
    Two adjacent doors cannot trip this.
    """
    if not item.text.strip().isdigit():
        return False
    value = int(item.text.strip())

    row = sorted(
        (o for o in items
         if o.text.strip().isdigit() and abs(o.cy - item.cy) <= _RUN_BASELINE_TOL),
        key=lambda o: o.x0,
    )
    if len(row) < _MIN_NUMBER_RUN:
        return False

    run = 1
    best = 1
    for previous, current in zip(row, row[1:]):
        if abs(int(current.text.strip()) - int(previous.text.strip())) == 1:
            run += 1
            best = max(best, run)
        else:
            run = 1
    if best < _MIN_NUMBER_RUN:
        return False
    return any(abs(int(o.text.strip()) - value) <= _MIN_NUMBER_RUN
               for o in row if o is not item)


def tagged_but_unscheduled(doc: PdfDoc, tags: set[str], sheets: list[PlanSheet]
                           ) -> list[UnscheduledDoor]:
    """Door-tag-shaped text on a plan that the schedule never mentions.

    This finds a door the schedule *forgot*, not a door drawn without a tag --
    for that there is nothing to read and the geometry or the vision tier has
    to answer. Say so rather than implying full coverage.
    """
    from app.core import door_locator as dl

    out: list[UnscheduledDoor] = []
    for sheet in sheets:
        items: list[TextItem] = [i for i in doc.text_items(sheet.page - 1) if i.text.strip()]
        size = dl._tag_size(items, tags)
        if size is None:
            continue
        for item in items:
            text = item.text.strip()
            if text in tags or not TAG_RE.match(text) or _is_not_a_door(text):
                continue
            if abs(item.size - size) > size * dl._SIZE_TOLERANCE:
                continue
            if dl._looks_like_a_room_number(item, items):
                continue
            if dl._in_a_reference_bubble(item, items):
                continue
            if _in_a_numbered_run(item, items):
                continue
            score, reasons = dl._company(item, items, tags)
            if score < _MIN_UNSCHEDULED_SCORE:
                continue
            out.append(UnscheduledDoor(text, sheet.page, sheet.number,
                                       item.x0, item.y0, item.x1, item.y1, reasons))
    return out


def reconcile(rows: list[DoorRow], sightings: list[DoorSighting],
              sheets: list[PlanSheet],
              unscheduled: list[UnscheduledDoor] | None = None) -> Reconciliation:
    """Schedule against plans."""
    found = [s for s in sightings if s.found]
    missing = [s for s in sightings if not s.found]

    result = Reconciliation(
        floor_plans=sheets, found=found, missing_from_plans=missing,
        unscheduled=list(unscheduled or []),
    )

    if not sheets:
        result.warnings.append(
            "No architectural floor plan sheet was identified, so no door could "
            "be located on a drawing. The schedule itself is unaffected."
        )
    ambiguous = [s.tag for s in found if s.confidence == "ambiguous"]
    if ambiguous:
        result.warnings.append(
            "These doors had more than one equally likely position on a sheet; "
            "check them against the drawing: " + ", ".join(sorted(ambiguous))
        )
    if missing:
        result.warnings.append(
            "These doors are scheduled but were not found on any floor plan: "
            + ", ".join(s.tag for s in missing)
        )

    _ = rows  # kept for signature stability; properties come from the schedule
    log.info("door_reconcile %s", result.summary)
    return result
