"""The two row-stitching failure modes, isolated from any PDF."""

from app.core.pdf_doc import TextItem
from app.core.row_builder import build_rows
from app.core.table_locator import TableGrid

COLS = [0.0, 50.0, 150.0, 250.0, 400.0]  # tag | from | to | comments
TAG_COL = 0


def grid() -> TableGrid:
    return TableGrid("banded", COLS[0], COLS[-1], 0.0, 10.0, list(COLS))


def item(x: float, y: float, text: str) -> TextItem:
    return TextItem(x, y, x + 40, y + 10, text, 10.0, True)


def test_wrapped_cell_joins_into_the_row_above():
    items = [
        item(5, 20, "1"), item(55, 20, "HALL"), item(155, 20, "CONFERENCE"),
        item(155, 32, "ROOM"),
        item(5, 50, "2"), item(55, 50, "MENS"), item(155, 50, "HALL"),
    ]
    rows = build_rows(grid(), items, TAG_COL)
    assert len(rows) == 2
    assert rows[0][2] == "CONFERENCE ROOM"
    assert rows[1][0] == "2"


def test_unnumbered_row_with_real_content_survives():
    """An opening with no door number is a row, not a continuation. A naive
    "no tag means continuation" rule silently destroys it."""
    items = [
        item(55, 20, "RECEPTION"), item(155, 20, "HALL"), item(255, 20, "OPENING"),
        item(5, 50, "1"), item(55, 50, "HALL"), item(155, 50, "EXTERIOR"),
    ]
    rows = build_rows(grid(), items, TAG_COL)
    assert len(rows) == 2
    assert rows[0][0] == ""
    assert rows[0][1] == "RECEPTION"
    assert rows[1][0] == "1"


def test_sparse_tagless_line_is_a_continuation():
    items = [
        item(5, 20, "1"), item(55, 20, "HALL"), item(255, 20, "FOLLOW MANU."),
        item(255, 32, "INSTRUCTIONS."),
    ]
    rows = build_rows(grid(), items, TAG_COL)
    assert len(rows) == 1
    assert rows[0][3] == "FOLLOW MANU. INSTRUCTIONS."


def test_items_outside_the_table_bounds_are_ignored():
    items = [
        item(5, 20, "1"), item(55, 20, "HALL"),
        item(900, 20, "TITLE BLOCK"),  # right of the table
    ]
    rows = build_rows(grid(), items, TAG_COL)
    assert len(rows) == 1
    assert "TITLE BLOCK" not in " ".join(rows[0])
