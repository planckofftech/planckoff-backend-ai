import pytest

from app.core.pdf_doc import NotAPdfError
from app.pipeline import NoScheduleFoundError, extract
from app.schemas import ExtractionMethod


@pytest.mark.asyncio
async def test_golden_fixture_matches_field_for_field(ellis_p21_bytes, expected):
    """The regression gate. Without this there is no way to tell an improvement
    from a regression."""
    result = await extract(ellis_p21_bytes, allow_ai=False)

    assert result.method == ExtractionMethod.DETERMINISTIC_RULED
    assert result.row_count == expected["row_count"] == 23
    assert result.headers == expected["headers"]

    for actual, want in zip(result.rows, expected["rows"], strict=True):
        assert actual.model_dump() == want


@pytest.mark.asyncio
async def test_unnumbered_opening_is_the_first_row(ellis_p21_bytes):
    result = await extract(ellis_p21_bytes, allow_ai=False)
    first = result.rows[0]
    assert first.door_tag == ""
    assert first.from_space == "RECEPTION"
    assert first.to_space == "HALL"
    assert first.comments == "OPENING"


@pytest.mark.asyncio
async def test_wrapped_cells_are_joined(ellis_p21_bytes):
    result = await extract(ellis_p21_bytes, allow_ai=False)
    by_tag = {r.door_tag: r for r in result.rows}
    assert by_tag["4"].to_space == "CONFERENCE ROOM"
    assert by_tag["2"].comments == "FOLLOW MANU. INSTALLATION INSTRUCTIONS."


@pytest.mark.asyncio
async def test_comments_column_is_not_folded_into_hw(ellis_p21_bytes):
    """The 105 pt header/data offset trap: nearest-anchor mapping files every
    comment under HW."""
    result = await extract(ellis_p21_bytes, allow_ai=False)
    by_tag = {r.door_tag: r for r in result.rows}
    assert by_tag["22"].hw_set == "02"
    assert by_tag["22"].comments == "FOLLOW MANU. INSTALLATION INSTRUCTIONS."


@pytest.mark.asyncio
async def test_hardware_schedule_and_title_block_are_excluded(ellis_p21_bytes):
    """Three tables sit side by side on this sheet; only the middle one is ours."""
    result = await extract(ellis_p21_bytes, allow_ai=False)
    blob = " ".join(
        v for r in result.rows for v in r.model_dump().values() if isinstance(v, str)
    )
    assert "RTMDesign" not in blob        # title block
    assert "Arlington, Texas" not in blob  # title block
    assert len(result.headers) == 14


@pytest.mark.asyncio
async def test_full_bid_set_end_to_end(full_bid_set_bytes):
    """Definition of done: 23 rows from the 46 MB set, deterministic, page 21."""
    result = await extract(full_bid_set_bytes, allow_ai=False)
    assert result.pages_scanned == 102
    assert result.source_pages == [21]
    assert result.row_count == 23
    assert result.method == ExtractionMethod.DETERMINISTIC_RULED
    assert result.duration_ms < 5000


@pytest.mark.asyncio
async def test_corrupt_file_is_rejected_as_such():
    with pytest.raises(NotAPdfError):
        await extract(b"this is definitely not a pdf")


@pytest.mark.asyncio
async def test_pdf_without_a_schedule_says_so(no_schedule_pdf):
    with pytest.raises(NoScheduleFoundError) as exc:
        await extract(no_schedule_pdf, allow_ai=False)
    assert "No door schedule found" in str(exc.value)
    assert "1 page" in str(exc.value)


@pytest.mark.asyncio
async def test_small_doc_guess_is_never_reported_as_a_find(schedule_shaped_pdf):
    """On a document of <= 20 pages the pipeline nominates the best-scoring page
    for the AI tier even when nothing passed the gates. That is a guess. If it
    yields nothing, the error must not claim a schedule was located -- a message
    naming a page sends someone hunting a table that is not there.

    Regression: ASSEMBLIES.pdf (9 pages, zero occurrences of "DOOR") reported
    "Found a door schedule on page 4 but could not read any rows."
    """
    with pytest.raises(NoScheduleFoundError) as exc:
        await extract(schedule_shaped_pdf, allow_ai=False)

    message = str(exc.value)
    assert "No door schedule found" in message
    assert "Found a door schedule" not in message
