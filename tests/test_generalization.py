"""A second bid set, from a different firm, with a different column layout.

Every threshold in the page finder was fitted to the Ellis County set. This is
the test that distinguishes "tuned on one sample" from "actually works".
Skips when the file is absent -- bid sets are not committed.
"""

from pathlib import Path

import pytest

from app.pipeline import extract
from app.schemas import ExtractionMethod

GRACEM = Path(__file__).parent.parent / "GRACEM_2.PDF"


@pytest.fixture
def gracem_bytes() -> bytes:
    if not GRACEM.exists():
        pytest.skip(f"{GRACEM.name} not present")
    return GRACEM.read_bytes()


@pytest.mark.asyncio
async def test_second_bid_set_extracts_deterministically(gracem_bytes):
    result = await extract(gracem_bytes, allow_ai=False)

    assert result.method == ExtractionMethod.DETERMINISTIC_RULED
    assert result.pages_scanned == 70
    assert result.source_pages == [41]
    assert result.row_count == 39


@pytest.mark.asyncio
async def test_different_column_layout_still_maps(gracem_bytes):
    """This sheet has no FROM/TO and splits the frame into head/jamb/sill."""
    result = await extract(gracem_bytes, allow_ai=False)

    assert result.headers[0] == "DOOR"
    first = result.rows[0]
    assert first.door_tag == "101A"
    assert first.door_type == "H"
    assert first.door_material == "WOOD/STAIN"
    assert first.frame_material == "ALUM. CLAD"
    assert first.comments == "CARD READER"
    # Columns this firm has and Ellis does not are kept, not dropped.
    assert first.extra["frame_head_detail"] == "1/A.403"
    assert first.extra["door_glazing"] == "A"


ROTATED = Path(__file__).parent.parent / "door schdule.pdf"


@pytest.fixture
def rotated_bytes() -> bytes:
    if not ROTATED.exists():
        pytest.skip(f"{ROTATED.name} not present")
    return ROTATED.read_bytes()


@pytest.mark.asyncio
async def test_grouped_headers_are_qualified_by_their_group(rotated_bytes):
    """A /Rotate 90 sheet grouping columns under PANEL and FRAME. Both groups
    contain a MAT'L; without the group heading the second is a duplicate and
    frame_material comes back empty on all 128 rows."""
    result = await extract(rotated_bytes, allow_ai=False)

    assert result.row_count == 128
    assert "PANEL MAT'L" in result.headers
    assert "FRAME MAT'L" in result.headers

    first = result.rows[0]
    assert first.door_material == "HM"
    assert first.frame_material == "HM", "frame material lost to a duplicate header"
    assert first.door_width == "3' - 0\""
    # The frame's own width and thickness must not displace the door's fields.
    assert first.extra["frame_width"] == '2"'
    assert first.extra["panel_thk"] == '1 3/4"'


@pytest.mark.asyncio
async def test_alphanumeric_door_tags_survive(gracem_bytes):
    result = await extract(gracem_bytes, allow_ai=False)
    tags = [r.door_tag for r in result.rows]
    assert "101A" in tags and "129B" in tags
    assert all(t for t in tags), "no row should lose its tag"
