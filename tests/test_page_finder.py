import time

import pytest

from app.core import page_finder
from app.core.pdf_doc import PdfDoc


def test_finds_page_21_in_full_bid_set(full_bid_set_bytes):
    """The whole point of Phase 1: one page out of 102, no rendering, no AI."""
    with PdfDoc(full_bid_set_bytes) as doc:
        assert doc.page_count == 102
        scores = page_finder.find_schedule_pages(doc)

    hits = page_finder.passing(scores)
    assert [c.page for c in hits] == [21]

    winner = hits[0]
    assert winner.header_hits == 12
    assert winner.tag_run == 22  # one per door row
    assert abs(winner.tag_x - 1124.2) < 1.0


@pytest.mark.perf
def test_scan_meets_the_five_second_budget(full_bid_set_bytes):
    """PLAN.md acceptance: 102 pages in under 5 s.

    Marked `perf` and deselected by default: wall-clock measured while the rest
    of the suite (or anything else on the machine) competes for CPU says nothing
    about the requirement. Run deliberately with `pytest -m perf` on a quiet box.
    """
    started = time.perf_counter()
    with PdfDoc(full_bid_set_bytes) as doc:
        page_finder.find_schedule_pages(doc)
    elapsed = time.perf_counter() - started
    assert elapsed < 5.0, f"scan took {elapsed:.1f}s"


def test_page_19_sheet_index_is_rejected(full_bid_set_bytes):
    """Page 19 contains the literal string "DOOR SCHEDULE" and no table.
    Keyword matching alone would return it."""
    with PdfDoc(full_bid_set_bytes) as doc:
        scores = page_finder.find_schedule_pages(doc)
    by_page = {c.page: c for c in scores}
    assert not by_page[19].passed


def test_scores_every_page_not_just_winners(full_bid_set_bytes):
    """Per-page numbers are what let the thresholds be retuned from real data."""
    with PdfDoc(full_bid_set_bytes) as doc:
        scores = page_finder.find_schedule_pages(doc)
    assert len(scores) == 102
    assert {c.page for c in scores} == set(range(1, 103))


def test_single_page_fixture_scores_the_same(ellis_p21_bytes):
    with PdfDoc(ellis_p21_bytes) as doc:
        scores = page_finder.find_schedule_pages(doc)
    hits = page_finder.passing(scores)
    assert [c.page for c in hits] == [1]
    assert hits[0].tag_run == 22


def test_tag_regex_accepts_door_tags_and_rejects_prose():
    accept = ["1", "22", "A1", "101A", "3-1", "D.2", "12B"]
    reject = ["HALL", "EXTERIOR", "WOOD", "", "PAINTED", "H.M."]
    for tag in accept:
        assert page_finder.TAG_RE.match(tag), tag
    for text in reject:
        assert not page_finder.TAG_RE.match(text), text
