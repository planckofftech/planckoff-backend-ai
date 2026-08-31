"""Which sheet is which, in a set that runs to hundreds of pages.

Pipeline 1 asks one question of a document -- where is the door schedule -- and
answers it by scoring every page. Pipeline 2 needs something different: it has
to know that page 126 is `A2.10 GROUND LEVEL - FLOOR PLAN` and page 184 is
`E5.02`, because a door number found on an electrical sheet is not a door.

That distinction is not cosmetic. On one 187-page set, searching every page for
the 65 scheduled door numbers returned 270 hits on a single electrical sheet
and 111 on a structural one -- all of them dimensions and circuit numbers that
happen to read as `11`, `12`, `13`. Restricting the search to architectural
floor plans removes that noise at source rather than trying to filter it later.

The sheet number lives in the title block, which is the one part of a drawing
set that is laid out consistently: it is set larger than the body text and
matches a tight pattern (`A2.10`, `E5.02`, `S6.01`).
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass

from app.core.pdf_doc import PdfDoc

log = logging.getLogger(__name__)

# Sheet numbering is not standardised, and every set writes it differently:
# `A2.10` and `E5.02` on one, bare `A1` `A2` `G1` on another, `G-000` and
# `C03.01` on a third. Requiring a separator between the numbers -- which the
# first set happened to have -- found no architectural sheet at all on four of
# six real drawing sets.
#
# The trailing letter may be lower case: one set numbers its floor plans A300a
# and A300b, and an upper-case-only suffix rejected both. Its door numbers were
# then reported as missing from drawings they are printed on.
#
# Anchored, so a dimension or a note cannot match.
SHEET_NUMBER = re.compile(r"^[A-Z]{1,3}[-.]?\d{1,3}(?:[.\-]\d{1,3})?[A-Za-z]?$")
# Only the largest few spans on a page are title-block material. Reading every
# span would find `A2.10` inside a cross-reference in the middle of the drawing.
_TITLE_SPANS = 12
# The architectural discipline. Doors are drawn and scheduled here; other
# disciplines reference them but do not define them.
# Sheet-number prefixes whose drawings we treat as the architect's own.
#
# An allowlist, and deliberately. The alternative -- excluding the engineering
# disciplines -- fails open: a sheet whose number could not be read has an empty
# prefix, matches no exclusion, and is scanned. Measured on the BMK VE set that
# admits four sheets with no readable number, two of them title-block debris
# ("DATE:12.30.25 DRAWN BY: CHECKED BY:"). An allowlist fails closed instead:
# an unknown prefix is skipped, and the price is a list to extend.
#
# CR is why this exists at all. A clean-room package numbers its sheets CR1.00,
# and reading only the first letter called that Civil and threw away four floor
# plans -- with them, nineteen doors that were printed nowhere else.
_ARCHITECTURAL = frozenset({
    "A",     # the architectural set
    "AD",    # addendum / demolition, depending on the firm
    "AS",    # architectural site
    "AI",    # architectural interiors
    "ID",    # interior design
    "IN",    # interiors
    "CR",    # clean room -- its own package, with its own doors
})
# A sheet that draws doors in plan view says so in its own title.
_PLAN_WORDS = ("FLOOR PLAN", "ROOF PLAN", "SITE PLAN", "LEVEL", "PLAN")
# A plan of some other surface, or of another discipline's work. Never a floor
# plan, whatever else the title says -- "LEVEL 2 REFLECTED CEILING PLAN" names
# a level and is still a ceiling.
_NOT_A_FLOOR_PLAN = (
    "ROOF", "SITE", "LANDSCAPE", "CEILING", "RCP", "FRAMING", "FOUNDATION",
    "GRADING", "PAVING", "DRAINAGE", "UTILITY", "EROSION", "KEY PLAN",
    "HARDSCAPE",
    "PLAN DETAIL", "SLAB", "TRUSS", "PLUMBING", "MECHANICAL", "ELECTRICAL",
    # The building as it stands today, not as it will be built. Its doors are
    # the ones coming out, and the schedule is about the ones going in. One set
    # titles these "EXISTING CONDITIONS & DEMOLITION - FLOOR PLAN", so this
    # cannot be a rule the words FLOOR PLAN are allowed to overrule.
    "DEMOLITION", "DEMO PLAN", "EXISTING CONDITIONS",
    # The floor drawn again to show how people get out of it. Every door is on
    # it, drawn identically, so it is coverage we already have -- and it is the
    # commonest redraw there is: one set carries five EXIT PLAN sheets against
    # eight real plans, and they contributed 48 of 144 detections, all copies.
    #
    # Here rather than in `_ANOTHER_SUBJECT` because these titles name the floor
    # too -- "GROUND FLOOR EXIT PLAN" -- so a rule the floor test can overrule
    # would not catch them.
    "EXIT PLAN", "EGRESS", "SMOKE COMPARTMENT", "OCCUPANCY CLASSIFICATION",
    # A legend block, not a drawing. On a sheet with no bookmarks the title is
    # read off the page, and "CONSTRUCTION PLAN KEYNOTES" is set larger than
    # the drawing's own caption "CONSTRUCTION PLAN" right below the plan.
    "KEYNOTE", "LEGEND", "SYMBOLS", "GENERAL NOTES",
)
# The floor drawn again for somebody else's benefit. Every door is on these,
# drawn identically, so scanning one is not more coverage -- it is the same
# answer bought twice, and the two copies disagree: on one set door 127 came
# back `single_swing` from the floor plan and `pocket` from the furniture plan.
#
# Excluded unless the title also names the floor itself, because a set that
# combines the two into one drawing does exist: "FLOOR & LIGHTING PLAN" is one
# real project's only floor plan, and its 15 doors are on it.
_ANOTHER_SUBJECT = (
    # "SIGN LOCATION" spelled out because "SIGN" alone is inside "DESIGN".
    "FINISH", "FURNITURE", "FURNISHING", "EQUIPMENT", "FIXTURE", "SIGNAGE",
    "SIGN LOCATION",
    "LIFE SAFETY", "EGRESS", "POWER", "LIGHTING", "DATA", "SECURITY",
    "FIRE PROTECTION",
)
# A plan of one part of the building, drawn large. Every door on it is already
# on the overall floor plan, so it is a second copy of an answer we have --
# `_sheets_worth_scanning` would skip most of them anyway, but only after
# paying to work out where their doors are.
#
# `UNIT` is deliberately absent: a residential set draws each apartment type
# once and never draws its doors on the overall plan at all, so those
# enlargements are the only place those doors exist.
_ONE_PART_OF_IT = (
    "STAIR", "ELEVATOR", "ESCALATOR", "RESTROOM", "TOILET", "STOREFRONT",
    "CASHWRAP", "BREAK ROOM", "STOCK ROOM", "SHAFT", "VESTIBULE", "CLOSET",
)
# The word PLAN, not the letters. Substring matching let a title block's firm
# name -- "SPECIALIZED PLANNING & ARCHITECTURE" -- register as a plan on four
# sheets, and would do the same for PLANT, PLANE and PLANNED, all of which
# appear in the specification pages of these sets.
_SAYS_PLAN = re.compile(r"\bPLANS?\b")
# Which storey a sheet draws. A set names the same thing several ways -- "LEVEL
# 1", "LEVEL 01", "FIRST FLOOR" -- and a building's doors belong to a storey,
# not to a sheet number, so this is what the audit groups by.
_LEVEL = re.compile(
    r"\bLEVEL\s+0*(B?\d{1,2})\b"
    r"|\b(GROUND|FIRST|SECOND|THIRD|FOURTH|FIFTH)\s+FLOOR\b"
    r"|\b(BASEMENT|MEZZANINE|MEZZ)\b"
)
_ORDINAL = {"FIRST": "1", "SECOND": "2", "THIRD": "3", "FOURTH": "4",
            "FIFTH": "5"}
# A phase is not a storey, but it divides a job the same way: one renovation
# set draws the same floor three times, once per phase, and those are three
# different scopes of work rather than three copies of one answer.
_PHASE = re.compile(r"\bPHASE\s+([IVX]+|\d)\b")
# The floor drawn large, in part. Never the sheet to lead with when a plan of
# the whole storey exists -- it is a corner of the building without its context.
# A sheet that draws one part of the floor larger, so a congested area is
# readable. Presentation only: doors are searched for on every plan sheet
# whatever its title, and where a door appears on several the primary is chosen
# by arc, then scale -- not by this.
#
# PARTIAL is deliberately absent. A PARTIAL FLOOR PLAN is not an enlargement:
# it is the floor plan, cut across sheets because the building does not fit on
# one. The drawings say so themselves -- BMK's three partial sheets carry match
# lines ("continues on A3.11") and share one 1/8" scale, while its enlarged
# sheets have no match lines and are drawn at 1/4".
_ENLARGED = ("ENLARGED", "ENL. PLAN", "UNIT PLAN")
# A title that says the drawing is of the floor itself.
_IS_THE_FLOOR = ("FLOOR PLAN", "FLOOR &", "FLOOR AND", "PLAN - FLOOR")
# Subjects a sheet may carry alongside its floor plan without ceasing to be
# one. A ceiling is drawn beside the floor; a roof or a demolition is not.
_SHARES_THE_SHEET = ("CEILING", "RCP")
# Headings that belong to a block of notes beside a drawing, never to the
# drawing. Only used when reading a title off the page, which is the fallback
# for a set with no bookmarks.
#
# "DRAWING NUMBER" and the scale note are title-block *labels* -- they sit
# beside the sheet number in every set. Recognised off an image they merge with
# whatever is next to them, and the resulting blob is both large and contains
# the word "Plans", so it was winning the title over the sheet's real caption:
# "A.121 Floor Plan and Reflected Ceiling Plan" came back as
# "DRAWING NUMBER: A.121 Plans 01 Scole: 1/4"=1'0"".
_NOT_A_TITLE = ("KEYNOTE", "LEGEND", "SYMBOLS", "GENERAL NOTES", "NOTES:",
                "DRAWING NUMBER", "DRAWING NO", "SCALE", "SCOLE")
# Below this many usable outline entries the outline is a cover-page list, not
# a per-sheet index, and reading the pages is the honest fallback.
_MIN_BOOKMARKS = 5
# What a sheet title says when it carries a door schedule.
_DOOR_WORDS = ("DOOR", "OPENING", "LEAF")


@dataclass(slots=True)
class PlanSheet:
    page: int  # 1-indexed
    number: str  # "A2.10"
    title: str  # "GROUND LEVEL - FLOOR PLAN"

    @property
    def discipline(self) -> str:
        """The letters before the first digit: 'A3.10' -> 'A', 'CR1.00' -> 'CR'.

        The whole prefix, not the first character. One character reads CR as C
        and calls a clean-room package civil; it does the same to ID, FP, FA
        and EQ, turning them into I, F, F and E.
        """
        found = re.match(r"[A-Z]+", (self.number or "").upper())
        return found.group(0) if found else ""

    @property
    def is_architectural(self) -> bool:
        return self.discipline in _ARCHITECTURAL

    @property
    def level(self) -> str:
        """Which storey this sheet draws, named the same way every time.

        A door belongs to a floor of a building; it does not belong to a sheet
        number. One project draws its three storeys on five sheets and a reader
        asked for "the floor plan" means one of three things, not one of five.

        Empty where the title names no storey, which is most single-floor jobs
        and is not a failure -- it means "the building", and everything lands in
        one group as it should.
        """
        title = self.title.upper()
        found = _LEVEL.search(title)
        if found:
            number, ordinal, word = found.groups()
            if number:
                name = f"LEVEL {number}"
            elif ordinal:
                name = f"LEVEL {_ORDINAL[ordinal]}" if ordinal in _ORDINAL \
                    else ordinal
            else:
                name = "MEZZANINE" if word.startswith("MEZZ") else word
        else:
            name = ""
        phase = _PHASE.search(title)
        if phase:
            # Same floor, different scope of work. Keeping them apart is the
            # difference between three plans and three copies of one plan.
            return f"{name} PHASE {phase.group(1)}".strip()
        return name

    @property
    def is_enlargement(self) -> bool:
        """Is this a piece of the floor drawn large, rather than the floor?"""
        title = self.title.upper()
        return any(word in title for word in _ENLARGED)

    @property
    def is_floor_plan(self) -> bool:
        """Does this sheet draw the building in plan, with doors in it?

        Matching the words "FLOOR PLAN" was too narrow. Real sets title the
        same drawing "LEVEL 1 PLAN OVERALL", "FLOOR & LIGHTING PLAN",
        "ENLARGED PLAN - EAST" or "UNIT PLANS", and on one 187-page set the
        only titles containing "FLOOR PLAN" belonged to the *mechanical*
        sheets.

        So: any architectural sheet whose title says PLAN, minus the plans that
        are of something other than the floor.

        Two kinds of exclusion, and telling them apart matters. A ceiling plan
        or a foundation plan is a different surface and loses outright. A
        finish, furniture or power plan is *this* floor drawn again for another
        trade -- so it loses too, but only if the title does not also say it is
        the floor plan, because sets that combine the two exist.
        """
        title = self.title.upper()
        if not _SAYS_PLAN.search(title):
            return False
        # One sheet, two drawings. "FLOOR PLAN AND REFLECTED CEILING PLAN" is a
        # real title: the floor plan is drawn on the left of the sheet and the
        # ceiling plan on the right, and its doors are on the former. Rejecting
        # it for the word CEILING loses four sheets of floor plans on one set.
        #
        # Narrow on purpose -- it lets a ceiling through and nothing else. A
        # demolition or roof plan that also says FLOOR PLAN is still rejected,
        # because those are about a different set of doors or none at all.
        both = any(word in title for word in _IS_THE_FLOOR)
        for word in _NOT_A_FLOOR_PLAN:
            if word in title and not (both and word in _SHARES_THE_SHEET):
                return False
        if any(word in title for word in _ONE_PART_OF_IT):
            return False
        if any(word in title for word in _IS_THE_FLOOR):
            return True
        return not any(word in title for word in _ANOTHER_SUBJECT)


# Where a title block lives, as a fraction of the sheet: the right-hand strip
# on most sets, the bottom band on the rest. Only these are recognised, because
# reading a whole 34x22" sheet to find two lines of text is minutes of work for
# an answer that is always in the same corner.
# The corner the drawing number sits in, then the bottom band for sets that
# run their title block along the foot of the sheet. A corner is a quarter of
# the pixels of a full-height strip: 4.5s a page instead of 13.
_TITLE_STRIPS = ((0.74, 0.55, 1.0, 1.0), (0.0, 0.82, 1.0, 1.0))
# Pages per document we are willing to recognise while indexing. A flattened
# set is normally a dozen architectural sheets; a document that needs more than
# this is not one we should be reading a title block at a time.
_MAX_OCR_PAGES = 40


def _ocr_title_block(doc: PdfDoc, page: int) -> list[str]:
    """The title block of a page that carries no text, read as an image.

    A flattened sheet -- every glyph converted to outlines -- looks perfectly
    readable and reports zero text spans, so the sheet number and title are
    simply absent. On one set that is the entire architectural block, and the
    audit could then say nothing better than "no floor plan sheet was
    identified" about eleven sheets of floor plans.
    """
    from app.core import ocr

    if not ocr.available():
        return []
    seen = getattr(doc, "_ocr_pages", None)
    if seen is None:
        seen = doc._ocr_pages = set()
    if page not in seen and len(seen) >= _MAX_OCR_PAGES:
        return []
    seen.add(page)

    import fitz

    rect = doc.doc[page - 1].rect
    spans: list[str] = []
    for left, top, right, bottom in _TITLE_STRIPS:
        strip = fitz.Rect(rect.x0 + rect.width * left,
                          rect.y0 + rect.height * top,
                          rect.x0 + rect.width * right,
                          rect.y0 + rect.height * bottom)
        try:
            words = ocr.read(doc, page, clip=strip)
        except Exception as exc:  # noqa: BLE001 - unreadable is not fatal
            log.info("plan_index: could not read page %s as an image: %s",
                     page, exc)
            return []
        # Both readings, because they answer different questions. The lines
        # keep "A.121" standing alone, which is the only form the sheet-number
        # pattern can match; the stacked captions put "Floor Plan and" /
        # "Reflected" / "Ceiling Plan" back together, which is the only form
        # the title rules can match. Testing the number against the stacked
        # version rejected a perfectly good strip and fell through to a garbled
        # one, which is how "A.121 Floor Plan and Reflected Ceiling Plan"
        # became "CRAVING NUMEER: FN50K".
        # Captions first, so that when a joined caption and one of its own lines
        # are the same height the whole caption wins the tie. Ordered the other
        # way, "Floor Plan and Reflected Ceiling Plan" lost to its own last line
        # and the sheet was titled "Ceiling Plan" -- which reads as a ceiling
        # plan and was then excluded from the floor plans.
        lines = ocr.stitch_lines(words)
        captions = ocr.stack_block(lines)
        found = captions + [i for i in lines
                            if i.text not in {c.text for c in captions}]
        spans = [i.text for i in sorted(found, key=lambda i: -i.size)
                 if i.horizontal]
        if spans:
            log.info("plan_index: page %s read as an image (%d spans)",
                     page, len(spans))
            return spans
    return spans


def _largest_spans(doc: PdfDoc, page: int) -> list[str]:
    """The page's biggest text, largest first.

    Size is what separates the title block from the drawing: the sheet number
    is the largest thing on the sheet after the project name.
    """
    raw = doc.text_items(page - 1)
    if not raw:
        return _ocr_title_block(doc, page)[:_TITLE_SPANS]
    items = sorted(raw, key=lambda i: -i.size)
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = item.text.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= _TITLE_SPANS:
            break
    return out


def sheet_of(doc: PdfDoc, page: int) -> PlanSheet:
    """The sheet number and title of one page.

    Both may come back empty -- a cover sheet or a photo page has neither, and
    that is information, not a failure.
    """
    spans = _largest_spans(doc, page)
    number = next((s for s in spans if SHEET_NUMBER.match(s)), "")
    # A block of notes about the plan is not the plan. On a set with no
    # bookmarks the title is whatever is set largest, and one sheet prints
    # "CONSTRUCTION PLAN KEYNOTES" at 25 pt in the corner while the drawing's
    # own caption, "CONSTRUCTION PLAN", sits under the plan at 19 pt. Taking
    # the bigger one called a floor plan a keynote list, and the sheet holding
    # every door in the project was never looked at.
    title = next(
        (s for s in spans
         if (any(word in s.upper() for word in _PLAN_WORDS)
             or "SCHEDULE" in s.upper())
         and not any(word in s.upper() for word in _NOT_A_TITLE)),
        "",
    )
    if not title:
        # Not every sheet is a plan or a schedule. Take the largest span that is
        # not the sheet number itself, so a details sheet still gets a name.
        title = next((s for s in spans if s != number and len(s) > 4), "")
    return PlanSheet(page=page, number=number, title=title)


# A bookmark reads "A2.10 - LEVEL 1 PLAN OVERALL", sometimes with a sequence
# number in front: "067 A-610 - DOOR SCHEDULE".
_BOOKMARK = re.compile(
    r"^(?:\d+\s+)?"
    r"(?P<number>[A-Z]{1,3}[-.]?\d{1,3}(?:[.\-]\d{1,3})?[A-Za-z]?)"
    r"\s*[-–—:]\s*(?P<title>.+)$",
    re.IGNORECASE,
)


def from_bookmarks(doc: PdfDoc) -> list[PlanSheet]:
    """The sheet list read straight out of the PDF's outline.

    Five of six real sets carry one bookmark per sheet with its number, title
    and page. Reading them costs one call; rebuilding the same information by
    scanning the title block of every page costs 31 seconds on a 187-page set,
    and gets the title wrong often enough to lose whole projects -- one set's
    floor plans came back named "SUPPORTIVE HOUSING", which is the project.

    Returns [] when there is no outline, or when the outline is not per-sheet.
    The caller then falls back to reading the pages.
    """
    entries: list[tuple[str, str, int]] = []
    for title, page in doc.bookmarks():
        match = _BOOKMARK.match(title)
        if match:
            entries.append((match.group("number").upper(),
                            match.group("title").strip(), page))
    if not entries:
        return []

    # An outline often repeats the whole list against page 1 -- the index page's
    # own group. A page claimed by many different sheets is a table of contents,
    # not a sheet, so drop it rather than believing 125 sheets share page 1.
    claims: dict[int, set[str]] = defaultdict(set)
    for number, _title, page in entries:
        claims[page].add(number)
    crowded = {page for page, numbers in claims.items() if len(numbers) > 1}

    seen: set[str] = set()
    sheets: list[PlanSheet] = []
    for number, title, page in entries:
        if page in crowded or number in seen:
            continue
        seen.add(number)
        sheets.append(PlanSheet(page=page, number=number, title=title))

    log.info("plan_index read %d sheets from the PDF outline", len(sheets))
    return sorted(sheets, key=lambda s: s.page)


def index_sheets(doc: PdfDoc) -> list[PlanSheet]:
    """Every sheet, named -- from the outline if there is one, else by reading.

    The outline is exact and instant. Reading every page's title block is the
    fallback, and it is both slow and fallible; it stays because one real set
    in six has no outline at all.
    """
    from_toc = from_bookmarks(doc)
    if len(from_toc) >= max(_MIN_BOOKMARKS, doc.page_count // 3):
        return from_toc
    log.info("plan_index: no usable outline; reading %d title blocks",
             doc.page_count)
    return [sheet_of(doc, page) for page in range(1, doc.page_count + 1)]


def other_architectural(sheets: list[PlanSheet],
                        exclude: list[PlanSheet]) -> list[PlanSheet]:
    """The rest of the architectural set, for a second look.

    A sheet title is not reliable enough to filter on alone. Real sets label
    the largest text on the sheet with the project name or the issue stamp, so
    `A-103` through `A-106` came back as "SUPPORTIVE HOUSING" and `A-110A` as
    "ISSUE FOR PERMIT" -- all of them floor plans, none of them detectable by
    title, and 22 doors on one set and 13 on another were reported missing
    because of it.

    So: search the plainly-titled plans first, and only widen to these for the
    doors still unaccounted for. That keeps a clean set clean and stops an
    awkwardly-labelled one from failing outright.
    """
    seen = {s.page for s in exclude}
    return [
        s for s in sheets
        if s.is_architectural and s.page not in seen
        and "SCHEDULE" not in s.title.upper()
    ]


def schedule_sheets(sheets: list[PlanSheet]) -> list[PlanSheet]:
    """Architectural sheets whose own title says they carry a door schedule.

    The point is to stop searching. Scoring every page of a 187-page set to
    find the door schedule takes twelve seconds and can still land on the
    hardware matrix beside it; the sheet list already says `A7.10 - DOOR
    SCHEDULE`, so go straight there.

    A door/window schedule counts -- several sets combine the two -- and so
    does a hardware schedule sharing the sheet, because the extractor tells
    those apart once it is on the page.
    """
    found = [
        s for s in sheets
        if s.is_architectural
        and "SCHEDULE" in s.title.upper()
        and any(word in s.title.upper() for word in _DOOR_WORDS)
    ]
    log.info("plan_index schedule sheets: %s",
             [f"p{s.page} {s.number}" for s in found] or "none named")
    return found


def floor_plans(sheets: list[PlanSheet]) -> list[PlanSheet]:
    """The architectural sheets that draw the building in plan.

    Deliberately narrow. Elevations and details show doors too, but in another
    projection and mostly repeating what the plan already says; including them
    multiplies the reconciliation work for very little new information. On the
    reference set this is 5 sheets out of 187.
    """
    found = [s for s in sheets if s.is_architectural and s.is_floor_plan]
    log.info("plan_index floor plans: %s",
             [f"p{s.page} {s.number}" for s in found] or "none")
    return found
