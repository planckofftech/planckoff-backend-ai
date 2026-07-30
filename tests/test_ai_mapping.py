"""The AI path must not displace real values into the wrong fields.

Regression: the prompt injected our 14 canonical field names and asked the
model to fill them. On a sheet whose columns were OPNG / LOCK FUNCTION / THK /
FRAME TYPE, the model had nowhere to put the thickness, so `1 3/4"` landed in
door_material and every column after it shifted by one.

The model now transcribes the sheet's own headers and the shared header mapper
does the mapping, with `extra` as the escape hatch.
"""

import json

import pytest

from app.ai.response_parser import parse_table
from app.ai.vision_extract import rows_from_table

# The real sheet: FRAME OPENING SCHEDULE AND HARDWARE LIST.
SHEET_HEADERS = [
    "OPNG", "LOCK FUNCTION", "TYPE", "WIDTH", "HGT.", "THK.", "MAT'L", "HDW",
    "FRAME TYPE", "FRAME MAT'L", "HEAD", "JAMB", "THRES./SILL", "REMARKS",
]
SHEET_ROW = [
    "00", "LOBBY", "A", "6'-0\"", "7'-0\"", "1 3/4\"", "ALUM.", "1",
    "AL-2", "ALUM.", "3/A4.2", "2/A4.2", "3/A4.3", "2, 3, 11, 19, 26",
]


@pytest.mark.asyncio
async def test_thickness_does_not_become_the_door_material():
    rows, _ = await rows_from_table(SHEET_HEADERS, [SHEET_ROW])
    row = rows[0]

    assert row.door_material == "ALUM.", "MAT'L must be the material"
    assert row.door_material != '1 3/4"', "thickness displaced the material"
    # THK has no canonical home; it must be preserved, not dropped or promoted.
    assert row.extra.get("thk") == '1 3/4"'


@pytest.mark.asyncio
async def test_frame_columns_are_not_shifted():
    rows, _ = await rows_from_table(SHEET_HEADERS, [SHEET_ROW])
    row = rows[0]

    assert row.frame_material == "ALUM."
    # FRAME TYPE is not a material -- it belongs in extra, not frame_material.
    assert row.extra.get("frame_type") == "AL-2"


@pytest.mark.asyncio
async def test_unmappable_columns_are_preserved_not_dropped():
    rows, _ = await rows_from_table(SHEET_HEADERS, [SHEET_ROW])
    extra = rows[0].extra

    for key, value in [("lock_function", "LOBBY"), ("head", "3/A4.2"),
                       ("jamb", "2/A4.2")]:
        assert extra.get(key) == value, f"{key} was lost"


@pytest.mark.asyncio
async def test_short_rows_do_not_shift_remaining_cells():
    rows, _ = await rows_from_table(["#", "FROM", "TO", "COMMENTS"],
                                    [["1", "HALL", "OFFICE"]])
    row = rows[0]
    assert row.door_tag == "1"
    assert row.to_space == "OFFICE"
    assert row.comments == ""


def test_parses_the_headers_and_rows_shape():
    payload = json.dumps({"headers": ["#", "FROM"], "rows": [["1", "HALL"]]})
    headers, rows, warnings = parse_table(payload)
    assert headers == ["#", "FROM"]
    assert rows == [["1", "HALL"]]
    assert warnings == []


def test_parses_through_markdown_fences():
    payload = '```json\n{"headers": ["#"], "rows": [["1"]]}\n```'
    headers, rows, _ = parse_table(payload)
    assert headers == ["#"] and rows == [["1"]]


def test_recovers_rows_from_a_truncated_response():
    """A response cut off at the token limit is still worth most of its rows."""
    truncated = '{"headers": ["#", "FROM"], "rows": [["1", "HALL"], ["2", "MENS"], ["3", "OFF'
    headers, rows, warnings = parse_table(truncated)

    assert headers == ["#", "FROM"]
    assert rows == [["1", "HALL"], ["2", "MENS"]]
    assert any("truncated" in w for w in warnings)


def test_tolerates_rows_keyed_by_header():
    payload = json.dumps({"headers": ["#", "FROM"],
                          "rows": [{"#": "1", "FROM": "HALL"}]})
    headers, rows, _ = parse_table(payload)
    assert rows == [["1", "HALL"]]


def test_empty_response_is_reported_not_crashed():
    headers, rows, warnings = parse_table("")
    assert headers == [] and rows == []
    assert warnings
