"""Phase 2b -- assign text items to cells.

Items are placed by the column *band* they fall in, never by nearest header.
On the reference sheet the "Comments" header sits 105 pt right of its own data;
nearest-anchor mapping files every comment under "HW".
"""

from __future__ import annotations

from app.core.pdf_doc import TextItem
from app.core.table_locator import TableGrid


def _join(parts: list[tuple[float, float, str]]) -> str:
    """Order by line, then by x, and join with single spaces."""
    parts.sort(key=lambda p: (round(p[0] / 3), p[1]))
    return " ".join(p[2] for p in parts).strip()


def header_texts(grid: TableGrid, headers: list[TextItem],
                 items: list[TextItem]) -> list[str]:
    """One header string per column.

    Multi-line headers are stitched: "Frame" above "Finish" is one column named
    "Frame Finish", and it must not be read as a second bare "Finish".
    """
    if grid.mode == "ruled":
        band = [
            i for i in items
            if i.horizontal and grid.header_top - 0.5 <= i.cy <= grid.header_bottom + 0.5
        ]
    else:
        span = max((h.y1 - h.y0) for h in headers)
        top = min(h.y0 for h in headers) - span * 1.8
        band = [i for i in items if i.horizontal and top <= i.cy <= grid.header_bottom + 0.5]

    buckets: list[list[tuple[float, float, str]]] = [[] for _ in range(grid.n_cols)]
    for item in band:
        col = grid.column_of(item.x0)
        if col is None:
            col = grid.column_of(item.cx)
        if col is not None:
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
