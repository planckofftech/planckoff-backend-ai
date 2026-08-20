"""Phase 1 -- find the page(s) holding a door schedule.

Keyword matching alone returns 17 of 102 pages on the Ellis County set; page 19
contains the literal string "DOOR SCHEDULE" in a sheet index and no table. Two
structural tests cut that to exactly one page:

  1. some horizontal band carries >= 5 distinct header words
  2. >= 8 door-tag-like tokens share an x column below that band

This runs on the text layer only -- no rendering, no AI. It is the difference
between handing a vision model 1 page and handing it 102.
"""

from __future__ import annotations

import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, asdict

from app.core.pdf_doc import PdfDoc, TextItem

TAG_RE = re.compile(r"^(?=.*\d)[A-Z0-9]{1,6}(?:[-.][A-Z0-9]{1,4})?$", re.I)

HEADER_WORDS = (
    "WIDTH", "HEIGHT", "TYPE", "MATERIAL", "FINISH", "FRAME", "THRESHOLD",
    "RATING", "F.R", "F_R", "HW", "HARDWARE", "COMMENTS", "REMARKS", "MARK",
    "DOOR NO", "FROM", "TO", "GLAZ", "LOUVER", "SIZE", "THK",
    # Seen across the test corpus and previously unmatched entirely.
    "THICKNESS", "HGT", "PANEL", "LOCATION", "ROOM", "HEAD", "JAMB", "SILL",
    "LABEL", "DETAIL", "NOTES", "QTY", "GLAZING", "CORE", "GROUP", "SIGN",
)

# Word characters for splitting a heading. Apostrophes are separators so that
# MAT'L yields MAT, which prefix-matches MATERIAL. Underscores are kept, or the
# common F_R spelling of a fire-rating column splits into two useless letters.
_WORD_RE = re.compile(r"[A-Z0-9._]+")
# Abbreviations spelled out. An open "any 3-letter prefix" rule looked tidier
# but matched Col, Dia and Frm on a structural load table, which is how a
# beam schedule started scoring as a door schedule.
_ABBREVIATIONS = {
    "MAT": "MATERIAL", "MATL": "MATERIAL", "MTL": "MATERIAL",
    "THK": "THICKNESS", "THICK": "THICKNESS",
    "HGT": "HEIGHT", "HT": "HEIGHT",
    "WD": "WIDTH", "WID": "WIDTH",
    "FRM": "FRAME",
    "HDW": "HARDWARE", "HDWE": "HARDWARE",
    "RM": "ROOM", "LOC": "LOCATION",
    "DTL": "DETAIL", "DET": "DETAIL",
    "GLZ": "GLAZING", "THRESH": "THRESHOLD",
    "REM": "REMARKS", "FIN": "FINISH",
}
# A header cell is a label, not a sentence. Without this, matching keywords
# anywhere in the text scores specification pages: prose like "MATERIAL
# SUPPLIER, DIRECTLY TO THE ENGINEER OF RECORD" hits MATERIAL, and the A. B. C.
# clause letters look like a tag column.
_MAX_HEADER_CELL = 32
_MAX_HEADER_WORDS = 5

# Header text is bucketed at this granularity (pt) to tolerate baseline jitter.
_Y_BUCKET = 5.0
# Two tags belong to the same column if their left edges are within this (pt).
_TAG_X_TOL = 6.0
# Pages with fewer text items than this cannot hold a schedule.
_MIN_ITEMS = 40
# How far outside a header row's own width its data may sit (pt). Data is
# left-aligned under centre-aligned headers, so the tag column starts left of
# the first header cell.
_BAND_X_MARGIN = 80.0
# Bands per page given the full header-plus-tag-column test. Every page in the
# document goes through this, so the scan stays cheap.
_MAX_BANDS_SCORED = 6
# How far a stacked header may reach, and how many printed lines it may span.
# Kept deliberately tight: a wider window starts swallowing data rows, which
# on the Ellis set turned four extra pages into false positives.
_HEADER_STACK_SPAN = 40.0
_HEADER_STACK_ROWS = 3
# Two header rows this close describe one table, not two.
_SAME_TABLE_Y = 60.0
# ...and how near in x. Two views of one table put their tag column in exactly
# the same place; two tables side by side are a table's width apart, so this
# only has to be wider than a column and narrower than a table.
_SAME_TABLE_X = 60.0
# A gap in a row of headings wider than this is the space between two tables
# rather than the space between two columns. Adaptive, because a column on a
# 36-inch sheet is wider than one on a letter page: the row's own typical gap
# times the tolerance, held between the floor and the ceiling.
_BAND_GAP_TOLERANCE = 4.0
_MIN_BAND_GAP = 120.0
_MAX_BAND_GAP = 400.0
# How far outside a band's own width to look when collecting the heading rows
# stacked above or below it. Generous on purpose: a column headed only on the
# group row -- NOTES, HARDWARE GROUP, REMARKS -- sits beyond the last cell of
# the row beneath it, and on one sheet cutting those off removed the only word
# that proved the table was about doors at all.
_BAND_MERGE_MARGIN = 200.0


@dataclass(slots=True)
class PageCandidate:
    page: int  # 1-indexed
    header_hits: int
    header_y: float
    tag_run: int
    tag_x: float
    score: int
    passed: bool
    item_count: int = 0

    @property
    def has_text_layer(self) -> bool:
        """False when there is too little text to recover any structure from --
        a scanned sheet. Such a page is the AI tier's whole reason to exist."""
        return self.item_count >= _MIN_ITEMS

    def as_dict(self) -> dict:
        return asdict(self)


# Words only an opening has. Counting keywords alone is not enough: a
# structural bolt table lists Frm, Qty, Width, Thick and Loc, which is five
# generic hits and was passing as a door schedule. Requiring one of these makes
# the test about doors rather than about tables.
# The heading over a schedule's column of door numbers, as printed. A table has
# exactly one, so a second one in the same row means a second table -- see
# `_where_headings_repeat`.
#
# Matched against the cell's own text rather than through HEADER_WORDS, because
# these have to be recognised without also becoming keywords that raise every
# page's score. "NUMBER" alone is the giveaway: one real sheet stacks the
# heading as "Door" over "Number", so neither line says "DOOR NO".
_TABLE_START_TEXT = frozenset({
    "NO", "NUMBER", "DOOR NO", "DOOR NUMBER", "MARK", "DOOR MARK", "DR NO",
})


def _starts_a_table(text: str) -> bool:
    cleaned = re.sub(r"\s+", " ", text.strip().upper()).rstrip(".:")
    return cleaned in _TABLE_START_TEXT

_DOOR_MARKERS = frozenset({
    "DOOR NO", "THRESHOLD", "HARDWARE", "HW", "LOUVER", "GLAZ", "GLAZING",
    "JAMB", "SILL", "HEAD", "LABEL", "F.R", "F_R", "MARK",
})


def _header_words_found(texts: list[str]) -> set[str]:
    """Distinct HEADER_WORDS matched by this band.

    A keyword counts wherever it appears in the heading, not only at the start.
    Matching only the start meant "PANEL WIDTH" never matched WIDTH and "FIRE
    RATING" never matched RATING -- on one real sheet five headings should have
    matched and none did, so a table with 52 door numbers scored 3 of the 5
    needed and was discarded.

    Distinct, not total: a band of ten cells all reading "FINISH" is a legend,
    not a header row.
    """
    hits: set[str] = set()
    for raw in texts:
        text = re.sub(r"\s+", " ", raw.strip().upper())
        if not text or len(text) > _MAX_HEADER_CELL:
            continue
        tokens = set(_WORD_RE.findall(text))
        if len(tokens) > _MAX_HEADER_WORDS:
            continue
        for token in tokens:
            spelled = _ABBREVIATIONS.get(token)
            if spelled:
                hits.add(spelled)
        for word in HEADER_WORDS:
            if word in hits:
                continue
            if " " in word or "." in word:
                if word in text:  # "DOOR NO", "F.R"
                    hits.add(word)
            elif word in tokens:
                hits.add(word)
    return hits


def _header_word_hits(texts: list[str]) -> int:
    return len(_header_words_found(texts))


def split_row_into_tables(row: list[TextItem]) -> list[list[TextItem]]:
    """Split one line of text into the separate tables it belongs to.

    A sheet routinely carries two schedules side by side at the same height --
    DOOR SCHEDULE on the left, DOOR HARDWARE on the right. Bucketing text by y
    alone makes those one band, and the merged band scores higher than either
    real one: on one project the merge scored 12 keyword hits against the door
    schedule's 5, so every page of that set read the hardware matrix and the
    door schedule was never seen.

    Columns inside a table sit close together; a different table is far away.
    The threshold comes from the row's own spacing rather than a fixed number,
    because a column on a 36-inch sheet is wider than one on a letter page.
    """
    if len(row) < 2:
        return [row] if row else []

    ordered = sorted(row, key=lambda i: i.x0)
    gaps = sorted(b.x0 - a.x1 for a, b in zip(ordered, ordered[1:]) if b.x0 > a.x1)
    typical = gaps[len(gaps) // 2] if gaps else 0.0
    budget = min(_MAX_BAND_GAP, max(typical * _BAND_GAP_TOLERANCE, _MIN_BAND_GAP))

    groups: list[list[TextItem]] = []
    current = [ordered[0]]
    for previous, item in zip(ordered, ordered[1:]):
        if item.x0 - previous.x1 > budget:
            groups.append(current)
            current = []
        current.append(item)
    groups.append(current)
    return [part for group in groups for part in split_at_table_starts(group)]


def split_at_table_starts(row: list[TextItem]) -> list[list[TextItem]]:
    """Split a header row again wherever a column heading comes round twice.

    Spacing is not always enough to tell two tables apart. One real sheet
    prints two door schedules butted up against each other, so the gap between
    them is no wider than the gap between two columns inside either -- and the
    split above either merges them or cuts them in the wrong places. Merged,
    the pair reads as one table and the right-hand schedule's ten doors are
    never returned; cut wrongly, neither half keeps enough headings to qualify.

    But a table has exactly one column of door numbers. So a second one is not
    another column, it is the start of the next table -- which needs no
    spacing, no threshold and no tuning.

    Only the door-number heading counts. Splitting on any repeated heading was
    tried and it is wrong: a door schedule routinely carries MATERIAL and
    FINISH twice, once for the door and once for its frame, under DOOR and
    FRAME group headings. That rule cut one real schedule down the middle,
    leaving a left half with no door markers in it at all.
    """
    ordered = sorted(row, key=lambda i: i.x0)
    groups: list[list[TextItem]] = []
    current: list[TextItem] = []
    numbered = False

    for item in ordered:
        starts_a_table = _starts_a_table(item.text)
        if starts_a_table and numbered:
            groups.append(current)
            current = []
            numbered = False
        numbered = numbered or starts_a_table
        current.append(item)

    groups.append(current)
    return [g for g in groups if g]


def _scored_bands(horizontal: list[TextItem]
                  ) -> list[tuple[set[str], float, list[TextItem], list[TextItem]]]:
    """Every candidate header row as (words, header_y, cells, leaf cells), best
    first.

    Headings are routinely stacked -- DOOR and FRAME on one line, WIDTH and
    HEIGHT on the next -- and land in different y buckets. Scored separately
    neither reaches the gate; scored together they clear it easily. So each
    band is scored alone *and* merged with the band below it.

    Bands are also split across the page, because two schedules can share a
    height without sharing anything else -- see `split_row_into_tables`.

    `header_y` comes from whichever line carries more cells: that is the leaf
    row, and `table_locator.header_items()` uses this y to collect the cells
    that define the columns.
    """
    buckets: dict[int, list[TextItem]] = defaultdict(list)
    for item in horizontal:
        buckets[int(round(item.y0 / _Y_BUCKET))].append(item)

    keys = sorted(buckets)
    scored: list[tuple[set[str], float, list[TextItem], list[TextItem]]] = []

    for index, key in enumerate(keys):
        for group in split_row_into_tables(buckets[key]):
            # The stacked rows below have to be taken from this table's own
            # columns, not the whole width of the sheet, or splitting the first
            # row achieves nothing.
            lo = min(i.x0 for i in group) - _BAND_MERGE_MARGIN
            hi = max(i.x1 for i in group) + _BAND_MERGE_MARGIN

            merged = list(group)
            leaf = group
            top = min(i.y0 for i in group)
            scored.append((_header_words_found([i.text for i in merged]),
                           min(i.y0 for i in leaf), merged, leaf))

            # Group rows can sit well above the leaf row -- one sheet prints
            # LOCATION / PANEL / FRAME 88 pt above NO. / FROM / TO -- so merging
            # only the neighbouring bucket is not enough.
            for step in range(1, _HEADER_STACK_ROWS + 1):
                if index + step >= len(keys):
                    break
                below = [i for i in buckets[keys[index + step]]
                         if lo <= i.cx <= hi]
                if not below:
                    continue
                if min(i.y0 for i in below) - top > _HEADER_STACK_SPAN:
                    break
                merged = merged + below
                if len(below) >= len(leaf):
                    leaf = below
                scored.append((_header_words_found([i.text for i in merged]),
                               min(i.y0 for i in leaf), merged, leaf))

    scored.sort(key=lambda s: (-len(s[0]), s[1]))
    return scored


def _run_on(tags: list[TextItem], key) -> tuple[int, list[TextItem]]:
    ordered = sorted(tags, key=key)
    best_run = 0
    best: list[TextItem] = []
    start = 0
    for end in range(len(ordered)):
        while key(ordered[end]) - key(ordered[start]) > _TAG_X_TOL:
            start += 1
        if end - start + 1 > best_run:
            best_run = end - start + 1
            best = ordered[start:end + 1]
    return best_run, best


def _longest_x_run(tags: list[TextItem]) -> tuple[int, float]:
    """Size and left edge of the biggest cluster of tags sharing a column.

    Clustered on the left edge *and* on the centre, taking whichever finds more.
    A centred tag column puts "1" and "10" at different left edges, so left-edge
    clustering alone scored a column of 35 door numbers as a run of 3.
    """
    best_run = 0
    best: list[TextItem] = []
    for key in (lambda i: i.x0, lambda i: i.cx):
        run, members = _run_on(tags, key)
        if run > best_run:
            best_run, best = run, members
    # Report the left edge either way: downstream uses it as the column's start.
    return best_run, (min(i.x0 for i in best) if best else 0.0)


def score_page(items: list[TextItem], page_number: int, *,
               min_header_hits: int = 5, min_tag_run: int = 8) -> PageCandidate:
    horizontal = [i for i in items if i.horizontal]
    if len(horizontal) < _MIN_ITEMS:
        return PageCandidate(page_number, 0, 0.0, 0, 0.0, 0, False, len(horizontal))

    # Judge each candidate band on headers *and* its own tag column, then keep
    # the best. Taking only the band with most keywords lost pages where a
    # neighbouring schedule's header scored higher but had no tag column: on one
    # sheet a window schedule's header outscored the door schedule below it, so
    # the page was rejected on the window table's missing tag run.
    best: PageCandidate | None = None
    for words, header_y, band, leaf in _scored_bands(horizontal)[:_MAX_BANDS_SCORED]:
        candidate = _judge(horizontal, band, words, header_y, page_number,
                           min_header_hits, min_tag_run, leaf)
        if best is None or (candidate.passed, candidate.score) > (best.passed, best.score):
            best = candidate
    return best or PageCandidate(page_number, 0, 0.0, 0, 0.0, 0, False,
                                 len(horizontal))


def _judge(horizontal: list[TextItem], band: list[TextItem], words: set[str],
           header_y: float, page_number: int, min_header_hits: int,
           min_tag_run: int, leaf: list[TextItem] | None = None) -> PageCandidate:
    """Score one header band together with the tag column beneath it.

    The tag column is searched under the *leaf* row, not the merged band. A
    merged band reaches up into group headings, and where two schedules sit
    side by side on one sheet it reaches sideways into the neighbour: on one
    drawing the door schedule's header was paired with the room-finish
    schedule's Level column 1000 pt to its left, and the grid built from that
    pair contained neither table.
    """
    span = leaf or band
    x0 = min(i.x0 for i in span) - _BAND_X_MARGIN
    x1 = max(i.x1 for i in span) + _BAND_X_MARGIN
    tags = [i for i in horizontal
            if i.y0 > header_y + 1 and x0 <= i.x0 <= x1 and TAG_RE.match(i.text)]
    tag_run, tag_x = _longest_x_run(tags)

    hits = len(words)
    passed = (hits >= min_header_hits and tag_run >= min_tag_run
              and bool(words & _DOOR_MARKERS))
    return PageCandidate(
        page_number, hits, header_y, tag_run, tag_x,
        hits * 2 + min(tag_run, 30), passed, len(horizontal),
    )


def header_bands(items: list[TextItem], page_number: int, *,
                 min_header_hits: int = 5, min_tag_run: int = 8
                 ) -> list[PageCandidate]:
    """Every header row on the page, not just the strongest.

    One sheet routinely carries several schedules stacked down the page -- a
    main door schedule, then one for residential units, then one for guestrooms.
    Scoring only the best band reads one of them and silently drops the rest.
    """
    horizontal = [i for i in items if i.horizontal]
    if len(horizontal) < _MIN_ITEMS:
        return []

    found: list[PageCandidate] = []
    for words, header_y, band, leaf in _scored_bands(horizontal):
        if len(words) < min_header_hits:
            continue
        # One table yields several bands -- itself, and itself merged with the
        # group rows above it -- each with a different leaf row. They describe
        # the same table, so keep only the strongest. Bands are already sorted
        # by word count, so the first to claim a place is the best one.
        #
        # A table is identified by where it sits on *both* axes. Height alone
        # was enough for schedules stacked down a page, and silently wrong for
        # schedules printed side by side: those share a header row, so the
        # second was discarded as a repeat of the first. One real sheet prints
        # doors 101-117 in a left-hand table and 118-126 in a right-hand one,
        # both headed "Door Schedule" at the same height, and ten of its
        # twenty-five doors were never read.
        candidate = _judge(horizontal, band, words, header_y, page_number,
                           min_header_hits, min_tag_run, leaf)
        if any(abs(header_y - c.header_y) <= _SAME_TABLE_Y
               and abs(candidate.tag_x - c.tag_x) <= _SAME_TABLE_X
               for c in found):
            continue
        if candidate.passed:
            found.append(candidate)

    found.sort(key=lambda c: c.header_y)
    return found


def find_schedule_pages(doc: PdfDoc, *, min_header_hits: int = 5,
                        min_tag_run: int = 8) -> list[PageCandidate]:
    """Score every page. Returns all of them -- callers filter on `.passed`.

    Scoring every page (not just winners) is deliberate: the thresholds are
    fitted to one document, and only per-page numbers let them be retuned.
    """
    return [
        score_page(doc.text_items(i), i + 1,
                   min_header_hits=min_header_hits, min_tag_run=min_tag_run)
        for i in range(doc.page_count)
    ]


def passing(candidates: list[PageCandidate]) -> list[PageCandidate]:
    return sorted(
        (c for c in candidates if c.passed), key=lambda c: c.score, reverse=True
    )


def _cli(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python -m app.core.page_finder <file.pdf> [--all]")
        return 2
    show_all = "--all" in argv
    with open(argv[1], "rb") as fh:
        data = fh.read()
    started = time.perf_counter()
    with PdfDoc(data) as doc:
        results = find_schedule_pages(doc)
        elapsed = (time.perf_counter() - started) * 1000
        print(f"scanned {doc.page_count} pages in {elapsed / 1000:.1f}s")

    if show_all:
        print("\nall pages (header_hits / tag_run / score):")
        for c in sorted(results, key=lambda c: c.score, reverse=True):
            if c.score:
                print(f"  page {c.page:>4}  hits={c.header_hits:>2}  "
                      f"run={c.tag_run:>3}  score={c.score:>3}  "
                      f"{'PASS' if c.passed else ''}")

    hits = passing(results)
    print(f"\nPASSING PAGES: {[c.page for c in hits] or 'none'}")
    for c in hits:
        print(f"  page {c.page}  header_hits={c.header_hits}  header_y={c.header_y:.1f}  "
              f"tag_run={c.tag_run}  tag_x={c.tag_x:.1f}  score={c.score}")
    return 0 if hits else 1


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv))


