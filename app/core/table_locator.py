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

from dataclasses import dataclass, field

from app.core.pdf_doc import Rulings, Segment, TextItem

# Two rulings this close are the same line drawn twice.
_MERGE_TOL = 4.0
# A horizontal ruling must cover this fraction of the table width to be a row line.
_ROW_LINE_COVERAGE = 0.70
# A vertical ruling must cover this fraction of the header band to be a column line.
_COL_LINE_COVERAGE = 0.80
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

def _ruled_grid(headers: list[TextItem], rulings: Rulings) -> TableGrid | None:
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

    col_bounds = _dedupe([x for x in crossing if left <= x <= right], _MERGE_TOL)
    if len(col_bounds) < 4:
        return None

    grid = TableGrid("ruled", left, right, hdr_y0, hdr_y1, col_bounds)

    # Every header must land in its own column, or this is not our grid.
    seen: set[int] = set()
    for h in headers:
        col = grid.column_of(h.x0 if h.x0 >= left else left)
        if col is None:
            return None
        seen.add(col)
    if len(seen) < max(3, len(headers) - 2):
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

def locate_table(items: list[TextItem], rulings: Rulings, header_y: float,
                 tag_x: float) -> tuple[TableGrid, list[TextItem]]:
    """Returns the grid and the header cells it was built from."""
    headers = header_items(items, header_y, tag_x)
    grid = _ruled_grid(headers, rulings)
    if grid is not None:
        return grid, headers
    grid = _banded_grid(headers, items, header_y)
    grid.warnings.append(
        "no table rulings found; columns inferred from text alignment"
    )
    return grid, headers
