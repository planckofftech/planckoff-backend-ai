"""Phase 2c -- build rows when there are no rulings to build them from.

Two failure modes, both present on the reference sheet:

  Wrapped cells. "CONFERENCE" / "ROOM" and "FOLLOW MANU. INSTALLATION" /
  "INSTRUCTIONS." are one cell printed on two lines. They must be joined into
  the row above.

  A legitimately unnumbered row. The first data row is an opening with no door
  number: RECEPTION | HALL | 8'-0" | 7'-0" | OPEN | ... | OPENING. A naive
  "no number means continuation" rule silently deletes it.

Hence the rule: a line continues the row above only if it has no tag *and* is
sparse. A tagless line carrying a full complement of cells is its own row.
"""

from __future__ import annotations

from app.core.pdf_doc import TextItem
from app.core.table_locator import TableGrid

# A tagless line with more populated columns than this is a real row.
_MAX_CONTINUATION_CELLS = 2
# Row clustering tolerance as a fraction of the median glyph height.
_Y_TOL_FACTOR = 0.6


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2] if ordered else 0.0


def cluster_lines(items: list[TextItem]) -> list[list[TextItem]]:
    """Group items into printed lines by y."""
    if not items:
        return []
    heights = [i.y1 - i.y0 for i in items if i.y1 > i.y0]
    tol = max(_median(heights) * _Y_TOL_FACTOR, 1.0)

    ordered = sorted(items, key=lambda i: i.y0)
    lines: list[list[TextItem]] = [[ordered[0]]]
    for item in ordered[1:]:
        if item.y0 - lines[-1][0].y0 <= tol:
            lines[-1].append(item)
        else:
            lines.append([item])
    for line in lines:
        line.sort(key=lambda i: i.x0)
    return lines


def build_rows(grid: TableGrid, items: list[TextItem], tag_col: int) -> list[list[str]]:
    """Banded mode: cluster lines, then stitch continuations into their row."""
    body = [
        i for i in items
        if i.horizontal and i.y0 > grid.header_bottom
        and grid.column_of(i.x0) is not None
    ]
    if not body:
        return []

    rows: list[list[str]] = []
    gaps: list[float] = []
    prev_y: float | None = None

    for line in cluster_lines(body):
        cells = [""] * grid.n_cols
        for item in line:
            col = grid.column_of(item.x0)
            if col is None:
                continue
            cells[col] = f"{cells[col]} {item.text}".strip() if cells[col] else item.text

        populated = sum(1 for c in cells if c)
        has_tag = bool(cells[tag_col]) if 0 <= tag_col < grid.n_cols else False
        y = line[0].y0

        # A large vertical gap means the table ended and this is other artwork.
        if rows and prev_y is not None and gaps:
            if y - prev_y > _median(gaps) * 4:
                break

        if rows and not has_tag and populated <= _MAX_CONTINUATION_CELLS:
            for idx, text in enumerate(cells):
                if text:
                    rows[-1][idx] = f"{rows[-1][idx]} {text}".strip()
        else:
            rows.append(cells)
            if prev_y is not None:
                gaps.append(y - prev_y)
            prev_y = y

    return [r for r in rows if any(r)]
