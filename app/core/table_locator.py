"""Phase 2a -- find the table's bounds and its column geometry.

The sheet holds three things side by side: a hardware schedule on the left, the
door schedule in the middle, a title block on the right. "The page" is not "the
table", so locating horizontal bounds is a required step.

Two strategies, tried in order:

  ruled   -- take the column and row boundaries straight from the page's vector
             rulings. Exact, and it makes wrapped cells a non-problem: a cell
             that spills onto a second line is still inside the same ruled box.
  banded  -- no usable rulings, so infer columns by clustering the x of the
             left-aligned data. Deliberately NOT from header x: headers are
             centre-aligned and "Comments" sits 105 pt right of its own data,
             so both nearest-header and header-midpoint mapping put comments in
             the wrong column.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from app.core.pdf_doc import Rulings, Segment, TextItem

# Two rulings this close are the same line drawn twice.
_MERGE_TOL = 4.0
# A horizontal ruling must cover this fraction of the table width to be a row line.
_ROW_LINE_COVERAGE = 0.70
# A vertical ruling must cover this fraction of the header band to be a column
# line. Header text often overflows its ruled cell, so demanding most of the
# text's height rejects real grids: on one sheet the column rules covered 5.9 of
# an 11.1 pt header and every one of them was discarded.
_COL_LINE_COVERAGE = 0.45
# Header cells sit within this many pt of the detected header baseline.
_HEADER_BAND = 6.0
# Consecutive header cells further apart than this belong to different tables.
_MAX_COL_GAP = 320.0
# A neighbouring cell is part of the run if its gap is within this multiple of
# the run's typical gap.
_GAP_TOLERANCE = 4.0
# ...but never be stricter than this, or wide columns split a legitimate table.
_MIN_COL_GAP = 60.0
# Data x-positions within this are the same left-aligned column.
_DATA_X_TOL = 6.0
# A ruled row band taller than this multiple of the median is the end of the table.
_ROW_GAP_FACTOR = 3.0
# How far above the grid to look for a caption that is not ruled in (pt).
_FLOATING_TITLE_GAP = 34.0
# How near the tag column an item must start to count as a door number, and how
# many tagless rows may trail the last tagged one (a wrap, or an unnumbered
# opening) before the table is treated as finished.
_TAG_COLUMN_TOL = 14.0
# Zero, deliberately: in ruled mode a wrapped cell shares its row's box, so a
# tagless row at the *end* is the next table's caption, not more of this one.
_TAGLESS_TAIL = 0
# One printed line of headings shares a baseline this closely.
_HEADING_BASELINE_TOL = 3.0
# Heading rows that may sit above the sub-heading row before we stop climbing.
_MAX_HEADING_ROWS = 2
# A heading line names this many columns at least; fewer is a caption.
_MIN_HEADING_CELLS = 3
# ...and sits no further above the row beneath it than this (pt).
_HEADING_STACK_GAP = 20.0
# A heading is a label, not a sentence -- the same bound the page finder uses.
_MAX_HEADING_CELL = 32
# Row rules below the header that get a say in where the table's edges are,
# and how many of them must agree before we believe it.
_SPAN_WINDOW_ROWS = 8
_MIN_AGREEING_ROWS = 3
# How far above the headings the caption band may start, how many ruled strips
# to climb to reach it, and how much text it may hold before it is a table.
_MAX_CAPTION_BAND = 60.0
_MAX_CAPTION_RULES = 3
_MAX_CAPTION_CELLS = 6


@dataclass(slots=True)
class TableGrid:
    mode: str  # "ruled" | "banded"
    left: float
    right: float
    header_top: float
    header_bottom: float
    col_bounds: list[float]  # len == n_cols + 1
    row_bounds: list[float] = field(default_factory=list)  # ruled mode only
    warnings: list[str] = field(default_factory=list)

    @property
    def n_cols(self) -> int:
        return len(self.col_bounds) - 1

    def column_of(self, x: float) -> int | None:
        """Index of the column band containing x, or None if outside."""
        if x < self.col_bounds[0] or x >= self.col_bounds[-1]:
            return None
        lo, hi = 0, self.n_cols - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if x < self.col_bounds[mid]:
                hi = mid - 1
            elif x >= self.col_bounds[mid + 1]:
                lo = mid + 1
            else:
                return mid
        return None


class TableNotFoundError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# ruling helpers
# --------------------------------------------------------------------------- #

def _group_by_pos(segments: list[Segment], tol: float) -> list[tuple[float, list[Segment]]]:
    """Collapse segments onto shared positions, merging near-identical lines."""
    if not segments:
        return []
    ordered = sorted(segments, key=lambda s: s.pos)
    groups: list[tuple[float, list[Segment]]] = []
    bucket = [ordered[0]]
    for seg in ordered[1:]:
        if seg.pos - bucket[0].pos <= tol:
            bucket.append(seg)
        else:
            groups.append((sum(s.pos for s in bucket) / len(bucket), bucket))
            bucket = [seg]
    groups.append((sum(s.pos for s in bucket) / len(bucket), bucket))
    return groups


def _coverage(segments: list[Segment], lo: float, hi: float) -> float:
    """Length of [lo, hi] covered by the union of these segments.

    Union, not sum: rulings are routinely drawn as many short strokes, and
    summing them would let a densely hatched region masquerade as a long line.
    """
    spans = sorted(
        (max(s.start, lo), min(s.end, hi)) for s in segments if s.end > lo and s.start < hi
    )
    total, cursor = 0.0, lo
    for start, end in spans:
        if end <= cursor:
            continue
        total += end - max(start, cursor)
        cursor = max(cursor, end)
    return total


def _runs(segments: list[Segment], tol: float) -> list[list[float]]:
    """Merge segments into maximal touching runs.

    A ruling is routinely drawn one segment per cell, so a row's real extent is
    the chain of segments that touch, not any single segment.
    """
    out: list[list[float]] = []
    for start, end in sorted((s.start, s.end) for s in segments):
        if out and start <= out[-1][1] + tol:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return out


def _row_rule_span(horizontal: list[Segment], seed_x: float,
                   header_bottom: float) -> tuple[float, float] | None:
    """The table's left and right edge, measured from its own row rules.

    The header run cannot give this. Where a heading spans both rows of a
    stacked header -- REMARKS, DETAIL, the row-number column -- it sits on
    neither baseline, so it is not in the run, and bounds taken from the run
    stopped short of the table's own edges: on seven real sheets the last
    columns were outside the box and missing from the rows.

    The row rules have no such gap; every data row runs the full width. Take
    the run through the door-number column and the extent most of the first few
    rows agree on.

    The first few, rather than all of them: a second table below -- a hardware
    schedule, typically -- sits in the same columns, and being the longer of
    the two it wins any vote over the whole page. On one sheet that moved the
    table's left edge 280 pt inward and cost it four columns.

    Most of them, rather than all: one sheet draws a longer rule across the
    second row of the table, so demanding an unbroken run from the first row
    found nothing at all and the table lost its COMMENTS column.
    """
    hits: list[tuple[float, float]] = []
    for pos, segs in _group_by_pos(horizontal, _MERGE_TOL):
        if pos <= header_bottom - _MERGE_TOL:
            continue
        for start, end in _runs(segs, _MERGE_TOL):
            if start - _MERGE_TOL <= seed_x <= end + _MERGE_TOL and end - start > 50:
                # Quantise, or a half-point of drawing jitter breaks the streak.
                hits.append((round(start / 2) * 2.0, round(end / 2) * 2.0))
                break

    if not hits:
        return None
    # Counter keeps insertion order on ties, so the row nearest the header wins
    # when two tables each contribute half the window.
    span, count = Counter(hits[:_SPAN_WINDOW_ROWS]).most_common(1)[0]
    return span if count >= _MIN_AGREEING_ROWS else None


def _dedupe(values: list[float], tol: float) -> list[float]:
    out: list[float] = []
    for v in sorted(values):
        if not out or v - out[-1] > tol:
            out.append(v)
    return out


# --------------------------------------------------------------------------- #
# header row
# --------------------------------------------------------------------------- #

def header_items(items: list[TextItem], header_y: float, tag_x: float) -> list[TextItem]:
    """The header cells belonging to *this* table, left to right.

    Restricted to the run of cells containing the tag column, so a header-shaped
    band elsewhere on the sheet cannot widen the table.
    """
    band = sorted(
        (i for i in items
         if i.horizontal and abs(i.y0 - header_y) <= _HEADER_BAND),
        key=lambda i: i.x0,
    )
    if not band:
        raise TableNotFoundError("no header cells at the detected header line")

    anchor = min(range(len(band)), key=lambda k: abs(band[k].x0 - tag_x))

    # A fixed gap cannot separate "next column" from "unrelated block of notes
    # that happens to share this y". Grow the run using a gap budget derived
    # from the spacing already seen, so a run of tight columns stays tight.
    gaps = sorted(b.x0 - a.x1 for a, b in zip(band, band[1:]) if b.x0 > a.x1)
    typical = gaps[len(gaps) // 2] if gaps else _MAX_COL_GAP
    budget = min(_MAX_COL_GAP, max(typical * _GAP_TOLERANCE, _MIN_COL_GAP))

    start = anchor
    while start > 0 and band[start].x0 - band[start - 1].x1 < budget:
        start -= 1
    end = anchor
    while end + 1 < len(band) and band[end + 1].x0 - band[end].x1 < budget:
        end += 1
    return band[start:end + 1]


# --------------------------------------------------------------------------- #
# ruled mode
# --------------------------------------------------------------------------- #

def _heading_block_top(top: float, items: list[TextItem],
                       left: float, right: float) -> float:
    """Climb over the group headings stacked above the sub-heading row.

    A group heading -- DOOR above WIDTH / HEIGHT / MAT'L -- is a printed line
    of its own, and the first rule above the sub-headings is not the top of the
    header. Stopping there dropped every column headed only on the group row:
    SIGN, DETAIL and REMARKS came back as blank column names with their data
    still sitting in the table underneath.

    Climbing by printed line rather than by rule, because the two are not the
    same: one sheet rules the group row off from the sub-headings, another
    leaves group row and caption inside a single ruled band. What separates a
    heading line from a caption is that a heading line names several columns --
    a caption is one string, occasionally two with a note beside it.
    """
    above = sorted((i for i in items
                    if i.horizontal and i.cy < top
                    and left - 1 <= i.cx <= right + 1),
                   key=lambda i: -i.y0)
    for _ in range(_MAX_HEADING_ROWS):
        if not above:
            return top
        base = above[0].y0
        line = [i for i in above if base - i.y0 <= _HEADING_BASELINE_TOL]
        if (len(line) < _MIN_HEADING_CELLS
                or top - max(i.y1 for i in line) > _HEADING_STACK_GAP
                or any(len(i.text) > _MAX_HEADING_CELL for i in line)):
            return top
        top = min(i.y0 for i in line) - 2.0
        above = [i for i in above if i.y0 < base - _HEADING_BASELINE_TOL]
    return top


def _ruled_grid(headers: list[TextItem], items: list[TextItem],
                rulings: Rulings, tag_x: float) -> TableGrid | None:
    hdr_x0 = min(h.x0 for h in headers)
    hdr_x1 = max(h.x1 for h in headers)
    hdr_y0 = min(h.y0 for h in headers)
    hdr_y1 = max(h.y1 for h in headers)
    need = max((hdr_y1 - hdr_y0) * _COL_LINE_COVERAGE, 4.0)

    # Verticals that actually cross the header row -- these are column rules.
    crossing = [
        pos for pos, segs in _group_by_pos(rulings.vertical, _MERGE_TOL)
        if _coverage(segs, hdr_y0, hdr_y1) >= need
    ]
    if len(crossing) < 4:
        return None

    left = max((x for x in crossing if x <= hdr_x0 + 2), default=None)
    right = min((x for x in crossing if x >= hdr_x1 - 2), default=None)
    if left is None or right is None or right - left < 100:
        return None

    # Prefer the edges the row rules agree on. They both *widen* the grid --
    # picking up columns whose heading spans the stacked header rows, which the
    # header run never sees -- and *narrow* it, since the header run can reach
    # past the table into a notes block that happens to share its y.
    #
    # Taken as the edges themselves rather than snapped to the nearest column
    # rule: on one sheet the table's own right border is broken exactly where
    # the header crosses it, so it is not in `crossing` at all, and snapping
    # fell back to the next rule inward and dropped the REMARKS column.
    span = _row_rule_span(rulings.horizontal, tag_x, hdr_y1)
    if (span is not None and span[0] <= tag_x < span[1]
            and span[1] - span[0] >= 100 and span[0] <= hdr_x0 + _MAX_COL_GAP):
        left, right = span

    col_bounds = _dedupe(
        [left] + [x for x in crossing if left < x < right] + [right], _MERGE_TOL)
    if len(col_bounds) < 4:
        return None

    grid = TableGrid("ruled", left, right, hdr_y0, hdr_y1, col_bounds)

    # Header cells beyond the ruled bounds belong to whatever sits next to the
    # table -- a notes block sharing the header's y. Drop them rather than
    # rejecting the grid, which would fall back to guessing columns and pull
    # the notes in as data.
    inside = [h for h in headers if h.x0 >= left - 2 and h.x0 < right]
    if len(inside) < 3:
        return None

    # Every remaining header must land in its own column, or this is not our grid.
    seen: set[int] = set()
    for h in inside:
        col = grid.column_of(h.x0 if h.x0 >= left else left)
        if col is None:
            return None
        seen.add(col)
    if len(seen) < max(3, len(inside) - 2):
        return None

    # Row rules: horizontals spanning most of the table width.
    span = right - left
    row_ys = [
        pos for pos, segs in _group_by_pos(rulings.horizontal, _MERGE_TOL)
        if _coverage(segs, left, right) >= span * _ROW_LINE_COVERAGE
    ]
    row_ys = _dedupe(row_ys, _MERGE_TOL)

    header_top = max((y for y in row_ys if y <= hdr_y0 + 2), default=hdr_y0 - 2)
    header_bottom = min((y for y in row_ys if y >= hdr_y1 - 2), default=None)
    if header_bottom is None:
        return None
    header_top = _heading_block_top(header_top, items, left, right)

    below = [y for y in row_ys if y >= header_bottom - 0.5]
    if len(below) < 3:
        return None

    # Walk down until the rules stop. A sheet-wide line far below the last row
    # (a match line, a grid bubble strip) must not be swallowed as a table row.
    gaps = [b - a for a, b in zip(below, below[1:])]
    median_gap = sorted(gaps)[len(gaps) // 2]
    kept = [below[0]]
    for prev, cur in zip(below, below[1:]):
        if cur - prev > median_gap * _ROW_GAP_FACTOR:
            break
        kept.append(cur)
    if len(kept) < 3:
        return None

    grid.header_top = header_top
    grid.header_bottom = header_bottom
    grid.row_bounds = kept
    return grid


# --------------------------------------------------------------------------- #
# banded mode
# --------------------------------------------------------------------------- #

def _banded_grid(headers: list[TextItem], items: list[TextItem],
                 header_y: float) -> TableGrid:
    hdr_x0 = min(h.x0 for h in headers)
    hdr_x1 = max(h.x1 for h in headers)
    hdr_y1 = max(h.y1 for h in headers)
    # Data may start left of the leftmost header (centre-aligned headers) and
    # extends to the right edge of the widest header.
    lo, hi = hdr_x0 - 60.0, hdr_x1 + 60.0

    data = [i for i in items if i.horizontal and i.y0 > hdr_y1 and lo <= i.x0 <= hi]

    clusters: list[list[float]] = []
    for x in sorted(i.x0 for i in data):
        if clusters and x - clusters[-1][0] <= _DATA_X_TOL:
            clusters[-1].append(x)
        else:
            clusters.append([x])

    # A real column repeats down the page; a one-off is a stray annotation.
    threshold = max(2, len(data) // 200)
    starts = [c[0] for c in clusters if len(c) >= threshold]

    if len(starts) < 3:
        # No usable data columns -- fall back to midpoints between headers.
        anchors = [h.cx for h in headers]
        bounds = [hdr_x0 - 20.0]
        bounds += [(a + b) / 2 for a, b in zip(anchors, anchors[1:])]
        bounds.append(hdr_x1 + 20.0)
        return TableGrid(
            "banded", bounds[0], bounds[-1], min(h.y0 for h in headers), hdr_y1,
            _dedupe(bounds, 1.0),
            warnings=["column bands inferred from header centres; "
                      "left-aligned data columns were not detectable"],
        )

    left = starts[0] - 3.0
    right = max(hi, max(i.x1 for i in data) + 3.0)
    col_bounds = _dedupe([left] + starts[1:] + [right], 1.0)
    return TableGrid("banded", left, right, min(h.y0 for h in headers), hdr_y1, col_bounds)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def _is_note(text: str) -> str:
    """A note ruled into the caption strip is not the table's name."""
    return text.upper().lstrip().startswith("NOTE")


def table_title(grid: TableGrid, items: list[TextItem],
                rulings: Rulings) -> tuple[float | None, str]:
    """The table's caption, in the ruled band directly above the headers.

    It sits inside the same outer rule as the columns it names, so it is part
    of the table -- and it is what distinguishes several schedules stacked on
    one sheet from each other.
    """
    span = grid.right - grid.left
    if span <= 0:
        return None, ""

    boundaries = sorted(
        pos for pos, segs in _group_by_pos(rulings.horizontal, _MERGE_TOL)
        if _coverage(segs, grid.left, grid.right) >= span * _ROW_LINE_COVERAGE
        and pos < grid.header_top - 0.5
    )
    def caption(top: float, bottom: float | None = None) -> tuple[float | None, str]:
        band = [
            i for i in items
            if i.horizontal and top - 0.5 <= i.cy <= (bottom if bottom is not None
                                                      else grid.header_top) + 0.5
            and grid.left - 1 <= i.cx <= grid.right + 1
        ]
        if not band:
            return None, ""
        band.sort(key=lambda i: (round(i.cy / 3), i.x0))
        text = " ".join(i.text for i in band).strip()
        # A band full of column-ish text is another header row, not a caption.
        return (top, text) if 0 < len(text) <= 80 else (None, "")

    # Work up through the ruled strips above the headings. The nearest one is
    # not always the title: one sheet rules in "NOTE: REUSE EXISTING DOOR
    # HARDWARE WHERE POSSIBLE" between the caption and the columns, and that
    # note was being reported as the schedule's name.
    edges = boundaries + [grid.header_top]
    for index in range(len(boundaries) - 1, -1, -1):
        top, text = caption(edges[index], edges[index + 1])
        if not text or _is_note(text):
            continue
        # Return the top of this strip: everything below it down to the
        # headings -- the note included -- is part of the table.
        return top, text

    # Not every sheet rules its caption in, and the nearest rule above may bound
    # an empty strip. Fall back to looking a couple of lines further up.
    return caption(grid.header_top - _FLOATING_TITLE_GAP)


def table_top(grid: TableGrid, items: list[TextItem], rulings: Rulings) -> float:
    """The top of the whole printed table: its caption band, not its headings.

    The caption strip is inside the table's own outer rule, and on some sheets
    it carries a note next to the title -- "CONTRACTOR TO FURNISH AND INSTALL
    ALL DOORS, DOOR FRAMES, SIGNS AND HARDWARE" -- which is as much a part of
    the table as any column. Boxing only from the headings down cut it off.

    Climbing rule by rule, skipping empty strips: the rule immediately above
    the headings is often just a spacer with nothing in it.
    """
    span = grid.right - grid.left
    if span <= 0:
        return grid.header_top
    above = sorted(
        (pos for pos, segs in _group_by_pos(rulings.horizontal, _MERGE_TOL)
         if _coverage(segs, grid.left, grid.right) >= span * _ROW_LINE_COVERAGE
         and pos < grid.header_top - 0.5),
        reverse=True,
    )
    for rule in above[:_MAX_CAPTION_RULES]:
        if grid.header_top - rule > _MAX_CAPTION_BAND:
            break
        band = [i for i in items
                if i.horizontal and rule < i.cy < grid.header_top
                and grid.left - 1 <= i.cx <= grid.right + 1]
        if not band:
            continue  # a spacer strip; the caption may be above it
        # More than a caption's worth of text is the table above this one.
        return rule if len(band) <= _MAX_CAPTION_CELLS else grid.header_top
    return grid.header_top


def _trim_to_tagged_rows(grid: TableGrid, items: list[TextItem],
                         tag_x: float) -> None:
    """Cut the ruled rows off where the tag column stops.

    The row walk follows the rules downward and a neighbouring table's rules
    can be close enough in spacing to look like more of the same table -- on one
    sheet the schedule ran on into GLAZING TYPES and DEMOUNTABLE REQUIREMENTS
    below it. Those rows have no door number, and a door schedule's rows do.

    A couple of tagless rows are tolerated: a wrapped cell, or a genuine
    unnumbered opening, must not end the table.
    """
    bounds = grid.row_bounds
    if len(bounds) < 3:
        return

    from app.core.page_finder import TAG_RE

    # Which column holds the door numbers is decided here, from the data, not
    # taken from `tag_x`. The page finder measures the longest column of
    # tag-shaped text on the sheet, and on a sheet whose hardware schedule is
    # longer than its door schedule that column belongs to the hardware
    # schedule: on one drawing it pointed at HDWR, so the row walk found
    # "tags" all the way down and the table ran 6 rows past its last door.
    # `tag_x` still breaks ties, since it is usually right.
    rows = len(bounds) - 1
    tagged = [[False] * rows for _ in range(grid.n_cols)]
    for item in items:
        if not item.horizontal or not TAG_RE.match(item.text):
            continue
        column = grid.column_of(item.x0)
        if column is None or item.cy < bounds[0] or item.cy >= bounds[-1]:
            continue
        for index in range(rows):
            if bounds[index] <= item.cy < bounds[index + 1]:
                tagged[column][index] = True
                break

    counts = [sum(column) for column in tagged]
    if not any(counts):
        return
    best = max(range(grid.n_cols),
               key=lambda c: (counts[c], -abs(grid.col_bounds[c] - tag_x)))
    last = max(i for i, has in enumerate(tagged[best]) if has)
    keep = min(last + 1 + _TAGLESS_TAIL, len(bounds) - 1)
    if keep + 1 < len(bounds):
        grid.row_bounds = bounds[:keep + 1]


def locate_table(items: list[TextItem], rulings: Rulings, header_y: float,
                 tag_x: float) -> tuple[TableGrid, list[TextItem]]:
    """Returns the grid and the header cells it was built from."""
    headers = header_items(items, header_y, tag_x)
    grid = _ruled_grid(headers, items, rulings, tag_x)
    if grid is not None:
        _trim_to_tagged_rows(grid, items, tag_x)
        return grid, headers
    grid = _banded_grid(headers, items, header_y)
    grid.warnings.append(
        "no table rulings found; columns inferred from text alignment"
    )
    return grid, headers
