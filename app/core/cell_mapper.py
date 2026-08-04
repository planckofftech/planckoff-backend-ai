"""Phase 2b -- assign text items to cells.

Items are placed by the column *band* they fall in, never by nearest header.
On the reference sheet the "Comments" header sits 105 pt right of its own data;
nearest-anchor mapping files every comment under "HW".
"""

from __future__ import annotations

from app.core.pdf_doc import Rulings, TextItem
from app.core.table_locator import (
    _COL_LINE_COVERAGE,
    _MERGE_TOL,
    TableGrid,
    _coverage,
    _group_by_pos,
)


def _join(parts: list[tuple[float, float, str]]) -> str:
    """Order by line, then by x, and join with single spaces."""
    parts.sort(key=lambda p: (round(p[0] / 3), p[1]))
    return " ".join(p[2] for p in parts).strip()


def _spanned_columns(grid: TableGrid, vertical_groups, item: TextItem) -> list[int]:
    """Which columns this header cell covers, per the rulings at its own height.

    A group heading spans several columns, and the rulings say so: the sub-column
    rules simply do not extend up into the group row. Without this, a heading is
    filed under whichever column its left edge happens to land in -- producing
    "PANEL WIDTH" and "FRAME TYPE" while the sibling columns stay bare "MAT'L",
    indistinguishable from each other.
    """
    # Same threshold the grid itself was built with. A stricter one here sees
    # only the table's outer rules, so every header cell "spans" all columns.
    height = max(item.y1 - item.y0, 1.0)
    crossing = [
        pos for pos, segs in vertical_groups
        if _coverage(segs, item.y0, item.y1) >= height * _COL_LINE_COVERAGE
    ]
    left = max((x for x in crossing if x <= item.x0 + 1), default=None)
    right = min((x for x in crossing if x >= item.x1 - 1), default=None)
    if left is None or right is None:
        col = grid.column_of(item.x0)
        return [] if col is None else [col]

    cols = [
        k for k in range(grid.n_cols)
        if grid.col_bounds[k] >= left - 1 and grid.col_bounds[k + 1] <= right + 1
    ]
    if cols:
        return cols
    col = grid.column_of(item.x0)
    return [] if col is None else [col]


def header_texts(grid: TableGrid, headers: list[TextItem], items: list[TextItem],
                 rulings: Rulings | None = None) -> list[str]:
    """One header string per column.

    Multi-line headers are stitched: "Frame" above "Finish" is one column named
    "Frame Finish", and it must not be read as a second bare "Finish". Grouped
    headers are pushed down onto every column of their group, so a sheet with
    PANEL and FRAME groups yields "PANEL MAT'L" and "FRAME MAT'L" rather than
    two identical "MAT'L" columns.
    """
    if grid.mode == "ruled":
        # Rotated text is kept here, unlike everywhere else: a narrow column is
        # routinely headed sideways. Dropping those left six columns of one
        # sheet all called "PANEL", and the mapper then had nothing to tell
        # WIDTH from MATERIAL -- the values came back under the wrong names.
        band = [
            i for i in items
            if grid.header_top - 0.5 <= i.cy <= grid.header_bottom + 0.5
        ]
    else:
        span = max((h.y1 - h.y0) for h in headers)
        top = min(h.y0 for h in headers) - span * 1.8
        band = [i for i in items if i.horizontal and top <= i.cy <= grid.header_bottom + 0.5]

    vertical_groups = (
        _group_by_pos(rulings.vertical, _MERGE_TOL)
        if rulings is not None and grid.mode == "ruled" else None
    )

    buckets: list[list[tuple[float, float, str]]] = [[] for _ in range(grid.n_cols)]
    for item in band:
        if not item.horizontal:
            # A sideways heading spans its column's whole height, so the
            # group-span rule would read it as covering every column. It heads
            # exactly the column it sits in.
            col = grid.column_of(item.cx)
            columns = [] if col is None else [col]
        elif vertical_groups is not None:
            columns = _spanned_columns(grid, vertical_groups, item)
        else:
            col = grid.column_of(item.x0)
            if col is None:
                col = grid.column_of(item.cx)
            columns = [] if col is None else [col]
        for col in columns:
            buckets[col].append((item.y0, item.x0, item.text))
    return [_join(b) for b in buckets]


def cells_by_ruled_rows(grid: TableGrid, items: list[TextItem]) -> list[list[str]]:
    """Ruled mode: the boxes are drawn on the page, so just use them.

    Wrapped cells need no stitching rule here -- both lines are inside the same
    box, so they join automatically.
    """
    bounds = grid.row_bounds
    rows: list[list[list[tuple[float, float, str]]]] = [
        [[] for _ in range(grid.n_cols)] for _ in range(len(bounds) - 1)
    ]
    for item in items:
        if not item.horizontal:
            continue
        col = grid.column_of(item.x0)
        if col is None:
            continue
        cy = item.cy
        if cy < bounds[0] or cy >= bounds[-1]:
            continue
        lo, hi = 0, len(bounds) - 2
        row = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if cy < bounds[mid]:
                hi = mid - 1
            elif cy >= bounds[mid + 1]:
                lo = mid + 1
            else:
                row = mid
                break
        if row is not None:
            rows[row][col].append((item.y0, item.x0, item.text))

    out = [[_join(c) for c in row] for row in rows]
    return [r for r in out if any(c for c in r)]
