"""A table without column rules, read from its values first.

Where a schedule draws no column rules, the old reading took one line of
headings as the table's frame: the columns were where those headings stood,
and the door number column was whichever tall column of number-like values
stood under them. Every way that guess went wrong cost a schedule. Headings set
on staggered baselines -- MARK a few points above OPERATION, WIDTH a few below
-- left the heading line without MARK or WIDTH, the column of door styles
under it (F, FG4, FG4, FG4) was taken for the door numbers, and a sheet of 71
doors read as having none.

A table is read the way a person reads one, from what every schedule has:

  rows      one door per row, in a column of door numbers that all differ;
  columns   cells standing in line on those rows, with clear space between --
            whatever the headings above them look like;
  extent    the columns whose values sit on this table's rows. A legend or a
            neighbouring table beside it keeps its own line spacing, so it
            stops there without a rule for it;
  headings  the lines above the first row, each heading over the columns it
            spans.

The result is a `TableGrid` like any other, so cells, rows and headings are
taken from it by the code every table already goes through.
"""

from __future__ import annotations

import re
from bisect import bisect_left

from app.core.pdf_doc import TextItem, upright
from app.core.table_locator import TableGrid

# Door numbers standing within this of each other, by left edge or centre, are
# one column (points).
_COLUMN_TOL = 6.0
# Fewest door numbers, and fewest columns, that make a table worth reading
# this way.
_MIN_ROWS, _MIN_COLUMNS = 5, 4
# A door number column: values this short, and nearly all different.
_MAX_MARK, _DISTINCT_SHARE = 12, 0.9
# A value within this of a door number's line is on that row (points).
_ON_ROW_TOL = 2.5
# Share of a column's values that must sit on the table's rows for the column
# to be the table's. A legend beside it shares few.
_ON_ROWS_SHARE = 0.6
# A gap in the rows this many pitches wide ends the table.
_ROW_BREAK = 3.0
# A gap between columns this many times the table's usual gutter, and at
# least this wide, ends the table sideways.
_GUTTER_BREAK, _MIN_TABLE_GAP = 3.0, 40.0
# Heading lines stacked above the first row, at most, and how far apart two of
# them may be (in line heights).
_MAX_HEADING_LINES, _HEADING_GAP = 5, 1.5
# Share of a line's cells that must look like their column's values for a
# line without a door number to be a row of the table.
_VALUE_LIKE = 0.5

_SHAPE_DIGIT = re.compile(r"\d")
_SHAPE_LETTER = re.compile(r"[A-Za-z]")


def aligned_grid(items: list[TextItem], seed_x: float, seed_y: float
                 ) -> TableGrid | None:
    """The table standing around (seed_x, seed_y), or None if none reads.

    `seed_x` and `seed_y` say only where to look -- a column of door numbers
    near there, below a heading near there. Neither has to be right: the door
    number column is chosen from the table's own columns once they are found.
    """
    from app.core.page_finder import TAG_RE

    flat = [i for i in upright(items) if i.horizontal and i.text.strip()]
    below = [i for i in flat if i.cy > seed_y - _seek_above(flat)]
    tags = [i for i in below if TAG_RE.match(i.text.strip())]
    seed = _column_at(tags, seed_x)
    if len(seed) < _MIN_ROWS:
        return None

    table = _table_from(flat, tags, seed)
    if table is None:
        return None
    # The door numbers are the column of this table whose values all differ.
    # The seed may have been a column of styles standing taller.
    door = _door_column(flat, table)
    if door is not None and door is not seed and _distinct(door) > _distinct(seed):
        table = _table_from(flat, tags, door) or table
    return table.grid


def _seek_above(items: list[TextItem]) -> float:
    """How far above the seed line the first row may start: a heading's height."""
    heights = sorted(i.y1 - i.y0 for i in items)
    return 2.5 * heights[len(heights) // 2] if heights else 20.0


def _column_at(tags: list[TextItem], x: float) -> list[TextItem]:
    """The door-number-like values standing in one column at x."""
    by_left = [i for i in tags if abs(i.x0 - x) <= _COLUMN_TOL]
    if len(by_left) >= _MIN_ROWS:
        return sorted(by_left, key=lambda i: i.cy)
    near = min(tags, key=lambda i: abs(i.cx - x), default=None)
    if near is None:
        return []
    return sorted((i for i in tags if abs(i.cx - near.cx) <= _COLUMN_TOL),
                  key=lambda i: i.cy)


def _distinct(column: list[TextItem]) -> int:
    return len({i.text.strip().upper() for i in column})


class _Table:
    def __init__(self, grid: TableGrid, spans: list[tuple[float, float]],
                 rows: list[float]):
        self.grid, self.spans, self.rows = grid, spans, rows


def _rows_of(column: list[TextItem]) -> list[float]:
    """The lines of one run of door numbers, cut where the run breaks off."""
    ys = sorted({round(i.cy, 1) for i in column})
    if len(ys) < 2:
        return ys
    gaps = sorted(b - a for a, b in zip(ys, ys[1:]))
    pitch = gaps[len(gaps) // 2]
    runs, run = [], [ys[0]]
    for y in ys[1:]:
        if y - run[-1] > pitch * _ROW_BREAK:
            runs.append(run)
            run = []
        run.append(y)
    runs.append(run)
    return max(runs, key=len)


def _off_row(rows: list[float], y: float) -> float:
    k = bisect_left(rows, y)
    return min(abs(rows[j] - y) for j in (k - 1, k) if 0 <= j < len(rows))


def _spans(cells: list[TextItem], rows: int) -> list[tuple[float, float]]:
    """Columns as x-spans: runs of the page covered by cells, cut at gutters.

    A value that overruns into the next column -- a long comment, a material
    spelled out -- bridges the gutter on its own row only, so a gutter is
    where few rows have anything, not necessarily none.
    """
    if not cells:
        return []
    lo = int(min(i.x0 for i in cells))
    hi = int(max(i.x1 for i in cells)) + 1
    count = [0] * (hi - lo + 1)
    for i in cells:
        for x in range(int(i.x0) - lo, int(i.x1) - lo + 1):
            count[x] += 1
    stray = max(1, rows // 25)
    spans, start = [], None
    for x, n in enumerate(count):
        if n > stray and start is None:
            start = x
        elif n <= stray and start is not None:
            spans.append((start + lo, x + lo))
            start = None
    if start is not None:
        spans.append((start + lo, hi))
    return spans


def _table_from(flat: list[TextItem], tags: list[TextItem],
                door: list[TextItem]) -> _Table | None:
    rows = _rows_of(door)
    if len(rows) < _MIN_ROWS:
        return None
    gaps = sorted(b - a for a, b in zip(rows, rows[1:]))
    pitch = gaps[len(gaps) // 2]
    top, bottom = rows[0] - pitch * 0.5, rows[-1] + pitch * 0.5
    band = [i for i in flat if top <= i.cy <= bottom]
    on_rows = [i for i in band if _off_row(rows, i.cy) <= _ON_ROW_TOL]

    spans = _spans(on_rows, len(rows))
    door_x = sum(i.cx for i in door) / len(door)
    home = next((k for k, (a, b) in enumerate(spans) if a - 1 <= door_x <= b + 1), None)
    if home is None:
        return None

    def belongs(span: tuple[float, float]) -> bool:
        under = [i for i in band if i.x1 > span[0] and i.x0 < span[1]]
        if not under:
            return False
        aligned = sum(1 for i in under if _off_row(rows, i.cy) <= _ON_ROW_TOL)
        return aligned >= len(under) * _ON_ROWS_SHARE

    gutters = sorted(b[0] - a[1] for a, b in zip(spans, spans[1:]))
    usual = gutters[len(gutters) // 2] if gutters else _MIN_TABLE_GAP
    wide = max(_MIN_TABLE_GAP, usual * _GUTTER_BREAK)

    from app.core.page_finder import TAG_RE

    def starts_a_table(span: tuple[float, float]) -> bool:
        """A column of door numbers of its own: another table's first column."""
        values = [i.text.strip() for i in on_rows if span[0] - 1 <= i.cx <= span[1] + 1]
        return (len(values) >= len(rows) * 0.8
                and all(TAG_RE.match(v) for v in values)
                and len(set(values)) >= len(values) * 0.9)

    def joins(far: tuple[float, float], gap: float) -> bool:
        # Clear space alone does not end a table -- a comments column can stand
        # a hundred points past a narrow one. Clear space before a column of
        # door numbers of its own does: that is the next schedule.
        if gap > wide and starts_a_table(far):
            return False
        return belongs(far)

    first = last = home
    while first > 0 and joins(spans[first - 1], spans[first][0] - spans[first - 1][1]):
        first -= 1
    while last + 1 < len(spans) and joins(spans[last + 1],
                                          spans[last + 1][0] - spans[last][1]):
        last += 1
    spans = spans[first:last + 1]
    # Too few columns is not a table read: a schedule that gives each door a
    # block of lines -- its number once, then a line per piece of hardware --
    # has most of its values off the door numbers' lines, and only the number
    # column stands on them. That shape is left to the heading-led reading.
    if len(spans) < _MIN_COLUMNS:
        return None
    left, right = spans[0][0] - 2.0, spans[-1][1] + 2.0

    lines = _lines([i for i in flat if left <= i.cx <= right and i.cy < rows[0] - 1])
    body_top = _body_top(lines, spans, on_rows, rows, pitch)
    heading = _heading_lines([ln for ln in lines if max(i.y1 for i in ln) <= body_top + 1],
                             body_top)
    header_top = min((i.y0 for ln in heading for i in ln), default=body_top - pitch)

    spans = _with_empty_columns(spans, [i for ln in heading for i in ln
                                        if left <= i.cx <= right])
    bounds = [left] + [(a[1] + b[0]) / 2 for a, b in zip(spans, spans[1:])] + [right]
    grid = TableGrid("aligned", left, right, header_top, body_top, bounds,
                     spans=spans, bottom=rows[-1] + pitch * 0.5 + _last_row_lines(
                         flat, spans, rows, pitch))
    return _Table(grid, spans, rows)


def _with_empty_columns(spans: list[tuple[float, float]],
                        headings: list[TextItem]) -> list[tuple[float, float]]:
    """Add the columns that are headed but hold no values on this sheet.

    ELECTRIC STRIKE heads a column no door on one sheet fills, so no values
    stand in it, and its heading was pushed onto COMMENTS beside it. A heading
    with nothing under it -- no values, and no heading below it either -- is a
    column of its own. One with headings below it, like DOOR over OPERATION
    and STYLE, is a group over them.
    """
    loose = [h for h in headings
             if not any(h.x1 > a and h.x0 < b for a, b in spans)
             and not any(o is not h and o.y0 > h.y0 + 1 and o.x1 > h.x0 and o.x0 < h.x1
                         for o in headings)]
    added: list[list[float]] = []
    for h in sorted(loose, key=lambda i: i.x0):
        if added and h.x0 <= added[-1][1]:
            added[-1][1] = max(added[-1][1], h.x1)
        else:
            added.append([h.x0, h.x1])
    # Stacked words of one heading -- ELECTRIC over STRIKE -- are one column.
    merged: list[list[float]] = []
    for a, b in added:
        if merged and a <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return sorted(spans + [(a, b) for a, b in merged])


def _last_row_lines(flat: list[TextItem], spans: list[tuple[float, float]],
                    rows: list[float], pitch: float) -> float:
    """Room below the last door's line for its own wrapped lines -- a second
    line of comment -- and no further: below it the title block begins.

    A wrapped line is close under the one before it and sparse: one or two
    cells continuing the row. A full line, or one a row's height away, is not
    this door's.
    """
    left, right = spans[0][0], spans[-1][1]
    under = [i for i in flat if i.cy > rows[-1] + _ON_ROW_TOL and left <= i.cx <= right]
    reach = rows[-1]
    for line in reversed(_lines(under)):
        if min(i.y0 for i in line) - reach > pitch or len(line) > 2:
            break
        reach = max(i.cy for i in line)
    return max(0.0, reach - rows[-1] - pitch * 0.5) + 1.0


def _door_column(flat: list[TextItem], table: _Table) -> list[TextItem] | None:
    """The column of this table whose values all differ, across its rows.

    Known by identity, not spelling: one set numbers its doors D-153:1, which
    looks like no door number, and the column it stands in is still the one
    whose every value is different -- more of them than any other column, so a
    key of seven door types beside it is not taken. Not across every line: a
    schedule that lists each door's hardware on lines of its own gives a door
    several lines and its number once.
    """
    # Its whole run down the page, not only the rows the seed covered: a column
    # of styles breaks off where a door has none, and the door numbers go on.
    best: list[TextItem] | None = None
    pitch = (table.rows[-1] - table.rows[0]) / max(len(table.rows) - 1, 1)
    for a, b in table.spans:
        column = [i for i in flat if a - 1 <= i.cx <= b + 1
                  and i.cy >= table.rows[0] - 2 * pitch
                  and len(i.text.strip()) <= _MAX_MARK]
        if len(column) < _MIN_ROWS:
            continue
        if _distinct(column) < len(column) * _DISTINCT_SHARE:
            continue
        if best is None or _distinct(column) > _distinct(best):
            best = column
    return sorted(best, key=lambda i: i.cy) if best else None


def _lines(items: list[TextItem]) -> list[list[TextItem]]:
    """Printed lines, bottom first: items whose heights overlap share a line."""
    lines: list[list[TextItem]] = []
    for item in sorted(items, key=lambda i: -i.cy):
        if lines and item.y1 > min(i.y0 for i in lines[-1]) + 1 \
                and item.y0 < max(i.y1 for i in lines[-1]) - 1:
            lines[-1].append(item)
        else:
            lines.append([item])
    return lines


def _shape(text: str) -> str:
    return _SHAPE_LETTER.sub("A", _SHAPE_DIGIT.sub("9", text.strip().upper()))


def _body_top(lines: list[list[TextItem]], spans: list[tuple[float, float]],
              on_rows: list[TextItem], rows: list[float], pitch: float) -> float:
    """Where the table's body starts: the first door's row, or a row above it
    with no door number -- one schedule opens with an attic door that has
    none. A line counts when most of its cells look like the values of the
    columns they stand in, as headings never do.
    """
    seen: list[set[str]] = []
    for a, b in spans:
        values = [i.text for i in on_rows if a - 1 <= i.cx <= b + 1]
        seen.append({v.strip().upper() for v in values} | {_shape(v) for v in values})

    top = rows[0] - pitch * 0.5
    for line in lines:
        if top - max(i.y1 for i in line) > pitch:
            break
        hits = 0
        for item in line:
            k = next((n for n, (a, b) in enumerate(spans) if a - 1 <= item.cx <= b + 1), None)
            if k is not None and (item.text.strip().upper() in seen[k]
                                  or _shape(item.text) in seen[k]):
                hits += 1
        if hits < len(line) * _VALUE_LIKE or len(line) < 2:
            break
        top = min(i.y0 for i in line) - 1.0
    return top


def _heading_lines(lines: list[list[TextItem]], body_top: float
                   ) -> list[list[TextItem]]:
    """The heading: lines stacked close above the body, below the caption."""
    if not lines:
        return []
    heights = sorted(i.y1 - i.y0 for ln in lines for i in ln)
    height = heights[len(heights) // 2]
    taken: list[list[TextItem]] = []
    edge = body_top
    for line in lines:
        if edge - max(i.y1 for i in line) > height * _HEADING_GAP:
            break
        # A caption is one string set larger than the headings.
        if len(line) == 1 and line[0].y1 - line[0].y0 > height * 1.3:
            break
        taken.append(line)
        edge = min(i.y0 for i in line)
        if len(taken) >= _MAX_HEADING_LINES:
            break
    return taken
