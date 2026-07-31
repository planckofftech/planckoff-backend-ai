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


MULTI = Path(__file__).parent.parent / "DOOR SCHEDULE.pdf"


@pytest.fixture
def multi_schedule_bytes() -> bytes:
    if not MULTI.exists():
        pytest.skip(f"{MULTI.name} not present")
    return MULTI.read_bytes()


@pytest.mark.asyncio
async def test_every_schedule_on_the_sheet_is_returned(multi_schedule_bytes):
    """One sheet, three schedules stacked down the page, plus a notes block
    beside them. Reporting only the strongest header row dropped two of the
    three; letting the header run reach into the notes turned prose into
    columns."""
    result = await extract(multi_schedule_bytes, allow_ai=False)

    titles = [t.title for t in result.tables]
    assert titles == [
        "DOOR TYPE SCHEDULE",
        "DOOR TYPE SCHEDULE - RESIDENTIAL UNITS",
        "DOOR TYPE SCHEDULE - GUESTROOMS",
    ]
    assert [t.row_count for t in result.tables] == [65, 12, 7]
    assert result.method == ExtractionMethod.DETERMINISTIC_RULED

    # The notes block must not appear as columns or as data.
    blob = " ".join(
        h for t in result.tables for h in t.headers
    ) + " ".join(
        v for t in result.tables for r in t.rows
        for v in r.model_dump().values() if isinstance(v, str)
    )
    assert "EGRESS" not in blob, "notes bled into the table"
    assert "COMPLYING WITH IBC" not in blob


@pytest.mark.asyncio
async def test_field_map_follows_the_sheets_own_column_order(multi_schedule_bytes):
    """`field_map` aligns to `headers`, so a caller can render the table in the
    order the drawing prints it. Rendering canonical fields in our own fixed
    order put TYPE third on a sheet that prints it first."""
    result = await extract(multi_schedule_bytes, allow_ai=False)
    table = result.tables[0]

    assert len(table.field_map) == len(table.headers)
    assert table.headers[0] == "TYPE"
    assert table.field_map[0] == "door_type"
    # A column with no canonical equivalent is null, not silently shifted.
    assert table.field_map[table.headers.index("DOOR THICKNESS")] is None


@pytest.mark.asyncio
async def test_single_schedule_sheets_report_one_table(ellis_p21_bytes):
    result = await extract(ellis_p21_bytes, allow_ai=False)
    assert len(result.tables) == 1
    assert result.tables[0].title == "Door Schedule"
    assert result.tables[0].row_count == 23
    assert result.tables[0].rows == result.rows


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
