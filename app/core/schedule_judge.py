"""Is this table the door schedule? Weighed from every clue, never gated on one.

Each earlier rule settled the question alone, and each was right on the set it
was written for and wrong on the next. Eight door numbers in a column threw out
a renovation with seven doors whose sheet index, caption and twelve headings all
said "door schedule". A MARK column let an equipment schedule through, because
every schedule has one. And when every table on a page failed, the strongest
failure was handed over anyway -- which is how a PIPING SCHEDULE became seven
doors named D, DCW and NPW.

So every clue is weighed together:

    where       the sheet index calls this sheet a door schedule
    caption     the table's own title says door -- or names another schedule
    columns     door columns that actually hold values, not just headings
    sizes       the values read as door sizes: 3'-0" x 7'-0", 914 x 2032
    numbers     a named column of distinct door numbers, or one we had to guess
    trade       columns only another trade prints: CFM, REBAR, VOLTS

and the total decides Found, Unsure, or Reject. No one clue can carry a table
in or keep one out.

The reasons stay inside the Verdict, for tests and for whoever reads the code.
Nothing about them is logged or returned to a caller: a caller gets the outcome.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core import dimensions, extractor


FOUND = "found"
UNSURE = "unsure"
REJECT = "reject"

# At or above this the table is the door schedule; from the lower bound up to it,
# it might be. Measured against the confirmed answer key -- see
# scripts/score_detection.py -- not chosen.
_FOUND_AT = 6
_UNSURE_AT = 3

# What makes a sheet title, or a caption, about doors.
_DOOR_WORDS = ("DOOR", "OPENING")
# The columns that describe a door, as opposed to naming or locating it.
_DOOR_FIELDS = ("door_width", "door_height", "door_type", "door_material",
                "door_finish", "frame_material", "frame_finish", "fire_rating",
                "hw_set", "threshold")
# ...and the ones no window, storefront or louver schedule carries.
_DOOR_ONLY_FIELDS = ("frame_material", "frame_finish", "fire_rating", "hw_set",
                     "threshold")
# A cell that says nothing. An equipment schedule's HW column (hot water) is a
# column of X and -, and mapped onto the hardware set it filled a door field
# with ticks.
_EMPTY = {"", "-", "--", "–", "—", "X", "N/A", "NA", "TBD", "?"}
# A door column counts only when it is filled on at least this share of rows.
_FILLED_SHARE = 0.25
# Door sizes, in feet: a leaf narrower or a door taller than this is something
# else -- a beam, a pipe, a room.
_DOOR_FEET = (1.5, 14.0)
# Unitless millimetres, as metric sets print them: 914, 2032.
_DOOR_MM = (450, 4300)


@dataclass(slots=True)
class Verdict:
    outcome: str
    score: int
    reasons: list[str] = field(default_factory=list)


def says_door_schedule(title: str) -> bool:
    """A sheet title or caption naming a door schedule, in whatever order.

    "DOOR SCHEDULE", "SCHEDULE DOOR, LEGEND DOOR & FRAME", "DOOR & WINDOW
    SCHEDULE", "DOOR TYPES AND DOOR SCHEDULES" -- the words, not the phrase.
    """
    text = (title or "").upper()
    return "SCHEDULE" in text and any(w in text for w in _DOOR_WORDS)


# How strongly the set's own list of sheets points at a page.
NAMED = "named"          # a numbered sheet titled as a door schedule
SCHEDULES = "schedules"  # an architectural sheet titled just "schedules"
TITLE_ONLY = "title"     # a page whose title says door schedule, no number


def is_schedules_sheet(number: str, title: str) -> bool:
    """An architectural sheet of schedules that does not say which.

    "A7.01 SCHEDULES & MISC DETAILS" holds the door schedule on one set and
    nothing in its title says door. It is worth reading; it is not worth as
    much as a sheet that names the door schedule.
    """
    from app.core.plan_index import PlanSheet

    text = (title or "").upper()
    return ("SCHEDULE" in text and not says_door_schedule(text)
            and not extractor.is_other_schedule(text)
            and PlanSheet(page=0, number=number, title=title).is_architectural)


def door_sheet_pages(sheets) -> dict[int, str]:
    """Pages a list of sheets points at for the door schedule, and how strongly.

    Any discipline for a sheet titled as a door schedule: a set whose title
    blocks carry no readable sheet number has every sheet's discipline blank,
    and its door schedules were the two sheets dropped for it.

    But a title with no sheet number is weak evidence. A spec book has no
    sheet index, so its "titles" are whatever prints largest -- and a spec
    section that says "door schedule" in a heading read as an index entry,
    and a four-row clause list came back as the door schedule.
    """
    pages: dict[int, str] = {}
    for s in sorted(sheets, key=lambda s: s.page):
        if says_door_schedule(s.title):
            pages[s.page] = NAMED if s.number else TITLE_ONLY
        elif s.number and is_schedules_sheet(s.number, s.title):
            pages[s.page] = SCHEDULES
    return pages


def _meaningful(value: str) -> bool:
    return re.sub(r"\s+", " ", (value or "").strip()).upper() not in _EMPTY


def _is_door_size(value: str) -> bool:
    text = (value or "").strip()
    feet = dimensions.parse_feet(text)
    if feet is not None:
        return _DOOR_FEET[0] <= feet <= _DOOR_FEET[1]
    found = re.fullmatch(r"\(?\d?\)?\s*(\d{3,4})(?:\s*mm)?", text, re.I)
    return bool(found) and _DOOR_MM[0] <= int(found.group(1)) <= _DOOR_MM[1]


def judge(extraction, *, index_says_door: str | None) -> Verdict:
    """Found, Unsure or Reject for one table, with the reasons behind it.

    `index_says_door`: NAMED, SCHEDULES or TITLE_ONLY -- how strongly the set's
    list of sheets points at this page -- or None when it does not.
    """
    rows = extraction.rows
    title = re.sub(r"\s+", " ", (extraction.title or "").upper()).strip()
    score = 0
    why: list[str] = []

    def weigh(points: int, reason: str) -> None:
        nonlocal score
        score += points
        why.append(f"{points:+d} {reason}")

    caption_door = any(w in title for w in _DOOR_WORDS)
    # The table's own column headings. A caption can belong to the table next
    # door -- one door schedule came back captioned GLAZING SCHEDULE, its
    # neighbour's title, over columns reading DOOR SIZE / DOOR MATERIAL /
    # HARDWARE GROUP. Headings are printed inside the table; they cannot.
    headed_door = any(w in (h or "").upper() for h in extraction.headers
                      for w in _DOOR_WORDS)
    caption_other = not headed_door and (
        extractor.is_other_schedule(title)
        or ("SCHEDULE" in title and not caption_door))
    if headed_door:
        weigh(2, "column headings name a door")

    # Where. The index speaks for the sheet, not for every table on it: a
    # DOOR & WINDOW SCHEDULE sheet also carries the window schedule, and that
    # one's own caption says so.
    if index_says_door and not caption_other:
        if index_says_door == NAMED:
            weigh(3, "sheet index names a door schedule")
        else:
            weigh(1, f"sheet index points here weakly ({index_says_door})")

    # Caption.
    if caption_door:
        weigh(3, f"caption {title!r}")
    elif caption_other:
        weigh(-4, f"caption names another schedule {title!r}")

    # Columns that hold something. Where the caption names another schedule,
    # only the columns that schedule could not have count: a window schedule
    # has a width, a height and a type too, and those prove nothing about
    # doors. A frame, a fire rating, a hardware set or a threshold does.
    filled = []
    for name in (_DOOR_ONLY_FIELDS if caption_other else _DOOR_FIELDS):
        if name not in extraction.mapped or not rows:
            continue
        share = sum(_meaningful(getattr(r, name, "")) for r in rows) / len(rows)
        if share >= _FILLED_SHARE:
            filled.append(name)
    # A door schedule exists to describe doors. A list of numbers with notes
    # beside them -- a room list, a keynote list -- describes nothing.
    if filled:
        weigh(min(len(filled), 5), f"door columns with values: {filled}")
    else:
        weigh(-2, "no column describes a door")

    # Sizes that are door sizes -- unless another schedule is named, whose
    # openings are sized the same way.
    sized = [r for r in rows if _is_door_size(r.door_width) or _is_door_size(r.door_height)]
    if rows and len(sized) * 2 >= len(rows) and not caption_other:
        weigh(2, f"{len(sized)}/{len(rows)} rows carry a door size")

    # The door number column.
    guessed = extraction.tag_guessed
    if "door_tag" not in extraction.mapped:
        weigh(-2, "no door number column")
    elif guessed:
        weigh(-1, "door number column guessed from the data")
    else:
        weigh(1, "door number column named")
    # Distinct is what proves a door column -- one number per door. The shape
    # only adds to it: "D-153:1" is a door's ID as surely as "153A" is, and
    # penalising its colon threw out a schedule whose every row was a door.
    tags = [r.door_tag for r in rows if r.door_tag]
    if extractor._names_doors(rows):
        weigh(1, "door numbers distinct and number-shaped")
    elif len(tags) >= 2 and len(set(tags)) * 2 >= len(tags):
        weigh(0, "door numbers distinct, unusual shape")
    else:
        weigh(-2, "door numbers missing or repeating")

    # Another trade's columns.
    if extractor._names_another_trade(extraction.headers):
        weigh(-4, "columns only another trade prints")

    outcome = (FOUND if score >= _FOUND_AT
               else UNSURE if score >= _UNSURE_AT else REJECT)
    return Verdict(outcome, score, why)
