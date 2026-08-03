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


def _scored_bands(horizontal: list[TextItem]
                  ) -> list[tuple[set[str], float, list[TextItem]]]:
    """Every candidate header row as (words, header_y, cells), best first.

    Headings are routinely stacked -- DOOR and FRAME on one line, WIDTH and
    HEIGHT on the next -- and land in different y buckets. Scored separately
    neither reaches the gate; scored together they clear it easily. So each
    band is scored alone *and* merged with the band below it.

    `header_y` comes from whichever line carries more cells: that is the leaf
    row, and `table_locator.header_items()` uses this y to collect the cells
    that define the columns.
    """
    buckets: dict[int, list[TextItem]] = defaultdict(list)
    for item in horizontal:
        buckets[int(round(item.y0 / _Y_BUCKET))].append(item)

    keys = sorted(buckets)
    scored: list[tuple[int, float, list[TextItem]]] = []

    for index, key in enumerate(keys):
        merged = list(buckets[key])
        leaf = buckets[key]
        top = min(i.y0 for i in buckets[key])
        scored.append((_header_words_found([i.text for i in merged]),
                       min(i.y0 for i in leaf), merged))

        # Group rows can sit well above the leaf row -- one sheet prints
        # LOCATION / PANEL / FRAME 88 pt above NO. / FROM / TO -- so merging
        # only the neighbouring bucket is not enough.
        for step in range(1, _HEADER_STACK_ROWS + 1):
            if index + step >= len(keys):
                break
            band = buckets[keys[index + step]]
            if min(i.y0 for i in band) - top > _HEADER_STACK_SPAN:
                break
            merged = merged + band
            if len(band) >= len(leaf):
                leaf = band
            scored.append((_header_words_found([i.text for i in merged]),
                           min(i.y0 for i in leaf), merged))

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
    for words, header_y, band in _scored_bands(horizontal)[:_MAX_BANDS_SCORED]:
        candidate = _judge(horizontal, band, words, header_y, page_number,
                           min_header_hits, min_tag_run)
        if best is None or (candidate.passed, candidate.score) > (best.passed, best.score):
            best = candidate
    return best or PageCandidate(page_number, 0, 0.0, 0, 0.0, 0, False,
                                 len(horizontal))


def _judge(horizontal: list[TextItem], band: list[TextItem], words: set[str],
           header_y: float, page_number: int, min_header_hits: int,
           min_tag_run: int) -> PageCandidate:
    """Score one header band together with the tag column beneath it."""
    x0 = min(i.x0 for i in band) - _BAND_X_MARGIN
    x1 = max(i.x1 for i in band) + _BAND_X_MARGIN
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
    for words, header_y, band in _scored_bands(horizontal):
        if len(words) < min_header_hits:
            continue
        # One table yields several bands -- itself, and itself merged with the
        # group rows above it -- each with a different leaf row. They describe
        # the same table, so keep only the strongest. Bands are already sorted
        # by word count, so the first to claim a y-window is the best one.
        if any(abs(header_y - c.header_y) <= _SAME_TABLE_Y for c in found):
            continue
        candidate = _judge(horizontal, band, words, header_y, page_number,
                           min_header_hits, min_tag_run)
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


