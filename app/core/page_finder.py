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
)

# Header text is bucketed at this granularity (pt) to tolerate baseline jitter.
_Y_BUCKET = 5.0
# Two tags belong to the same column if their left edges are within this (pt).
_TAG_X_TOL = 6.0
# Pages with fewer text items than this cannot hold a schedule.
_MIN_ITEMS = 40


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


def _header_word_hits(texts: list[str]) -> int:
    """Distinct HEADER_WORDS matched by this band, by equality or prefix.

    Distinct, not total: a band of ten cells all reading "FINISH" is a legend,
    not a header row.
    """
    hits: set[str] = set()
    for raw in texts:
        t = raw.strip().upper()
        if not t:
            continue
        for word in HEADER_WORDS:
            if t == word or t.startswith(word) or word.startswith(t) and len(t) >= 2:
                hits.add(word)
    return len(hits)


def score_page(items: list[TextItem], page_number: int, *,
               min_header_hits: int = 5, min_tag_run: int = 8) -> PageCandidate:
    horizontal = [i for i in items if i.horizontal]
    if len(horizontal) < _MIN_ITEMS:
        return PageCandidate(page_number, 0, 0.0, 0, 0.0, 0, False, len(horizontal))

    # --- 1. best header band -------------------------------------------------
    buckets: dict[int, list[TextItem]] = defaultdict(list)
    for item in horizontal:
        buckets[int(round(item.y0 / _Y_BUCKET))].append(item)

    header_hits, header_y = 0, 0.0
    for key, band in buckets.items():
        hits = _header_word_hits([i.text for i in band])
        if hits > header_hits:
            header_hits = hits
            header_y = min(i.y0 for i in band)
        _ = key

    # --- 2. tag column below the header band ---------------------------------
    tags = [i for i in horizontal if i.y0 > header_y + 1 and TAG_RE.match(i.text)]
    tags.sort(key=lambda i: i.x0)

    tag_run, tag_x = 0, 0.0
    start = 0
    for end in range(len(tags)):
        while tags[end].x0 - tags[start].x0 > _TAG_X_TOL:
            start += 1
        run = end - start + 1
        if run > tag_run:
            tag_run = run
            tag_x = tags[start].x0

    passed = header_hits >= min_header_hits and tag_run >= min_tag_run
    score = header_hits * 2 + min(tag_run, 30)
    return PageCandidate(page_number, header_hits, header_y, tag_run, tag_x, score,
                         passed, len(horizontal))


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
