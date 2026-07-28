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
