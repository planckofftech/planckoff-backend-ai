import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
ELLIS_P21 = FIXTURES / "ellis_p21.pdf"
ELLIS_EXPECTED = FIXTURES / "ellis_p21_expected.json"

# The full 46 MB bid set is never committed. Tests that need it skip when absent.
FULL_BID_SET = Path(__file__).parent.parent / "1780995376_ELLIS_COUNTY_Bid Set.pdf"


@pytest.fixture(scope="session")
def ellis_p21_bytes() -> bytes:
    return ELLIS_P21.read_bytes()


@pytest.fixture(scope="session")
def expected() -> dict:
    return json.loads(ELLIS_EXPECTED.read_text(encoding="utf-8"))


@pytest.fixture
def full_bid_set_bytes() -> bytes:
    """Function-scoped on purpose: holding 46 MB (and the second bid set's
    63 MB) resident for the whole session slows every later test enough to
    break the timing assertions."""
    if not FULL_BID_SET.exists():
        pytest.skip(f"{FULL_BID_SET.name} not present")
    return FULL_BID_SET.read_bytes()


@pytest.fixture(scope="session")
def schedule_shaped_pdf() -> bytes:
    """Small document with a table-shaped page and no door schedule.

    Scores above zero (a tag-like column exists) but fails the header gate --
    the shape that triggers the small-document AI guess. Modelled on
    ASSEMBLIES.pdf, which is floor and ceiling assemblies.
    """
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=1200, height=900)
    page.insert_text((60, 60), "INTERIOR FLOOR ASSEMBLIES", fontsize=14)
    for row, tag in enumerate(f"FL{n:02d}" for n in range(10, 24)):
        y = 120 + row * 30
        page.insert_text((60, y), tag, fontsize=10)
        page.insert_text((160, y), "STRUCTURAL CONCRETE SLAB", fontsize=10)
        page.insert_text((520, y), "2 HOUR", fontsize=10)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture(scope="session")
def no_schedule_pdf() -> bytes:
    """A valid PDF with a text layer and no door schedule anywhere."""
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "GENERAL NOTES", fontsize=14)
    page.insert_text((72, 130), "All work shall comply with local codes.", fontsize=10)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def multi_schedule_bytes() -> bytes:
    path = Path(__file__).parent.parent / "DOOR SCHEDULE.pdf"
    if not path.exists():
        pytest.skip(f"{path.name} not present")
    return path.read_bytes()
