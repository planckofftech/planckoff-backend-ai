"""Thin PyMuPDF wrapper: positioned text, vector rulings, rendering.

All three layers come from one library, which is the whole reason this service
is Python and not an extension of the TypeScript app (PLAN.md section 3).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import fitz


class NotAPdfError(ValueError):
    pass


# Producers sometimes emit junk before the header, so look in a window rather
# than demanding it at byte zero.
_SIGNATURE_WINDOW = 2048


def _require_pdf_signature(path: Path) -> None:
    with path.open("rb") as handle:
        head = handle.read(_SIGNATURE_WINDOW)
    if b"%PDF" not in head:
        raise NotAPdfError("file does not start with a PDF signature")


@dataclass(slots=True)
class TextItem:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    size: float
    horizontal: bool

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass(slots=True)
class Segment:
    """An axis-aligned ruling line. `pos` is x for vertical, y for horizontal."""

    pos: float
    start: float
    end: float

    @property
    def length(self) -> float:
        return self.end - self.start


@dataclass(slots=True)
class Rulings:
    vertical: list[Segment]
    horizontal: list[Segment]


# A line is treated as axis-aligned within this tolerance (pt).
_STRAIGHT_TOL = 0.8
# Segments shorter than this are noise (hatching, arrowheads, glyph strokes).
_MIN_SEG_LEN = 2.0


# get_text("dict") decodes embedded images into the result by default. On a
# 46 MB bid set full of rasters that dominates the scan; we only want spans.
_TEXT_FLAGS = fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES

# Enough to cover the finder handing its candidates to the extractor.
_TEXT_CACHE_PAGES = 4

# How many straight pieces to cut a bezier into. One set draws every door swing
# as a single curve, and taking its chord -- one straight line where an arc was
# -- left the circle fitter nothing to fit: that project measured 0 swings.
#
# Four, measured rather than reasoned. More is not better, because each piece
# gets shorter and the pieces of an ordinary small curve fall through the
# `_MIN_SEG_LEN` noise floor, taking real arcs with them. Swings found:
#
#     steps       1     2     4     8
#     OBGYN      38    --    41    25
#     AT&T       37    37    38    38
#     BMK         0     2     2     2
_BEZIER_STEPS = 4

# Side of one cell in the per-page path index, in display points.
#
# Smaller cells reject more paths per query and cost more to hold, since a path
# is listed in every cell it crosses. Measured on a 65,415-path floor plan, over
# thirty 216 pt windows spread across the sheet:
#
#     cell        32     64    128    256    512
#     build      0.39   0.44   0.38   0.43   0.48   seconds, once per page
#     candidates  953   1096   1573   2204   4039   paths examined per query
#     query       7.0    7.9   10.3   13.1   23.1   ms
#
# 64 rather than 32: they are within a millisecond of each other at this window
# size, and 64 keeps the cell count down for the much wider windows the wall
# tagger asks for. Before the index the same query took 298 ms.
_GRID = 64.0


def _is_horizontal(direction: tuple[float, float], matrix: "fitz.Matrix") -> bool:
    """Is this text line horizontal *as displayed*?

    `get_text()` reports direction in unrotated PDF space, so on a /Rotate 90
    sheet the visually horizontal text reads as (0, -1). Rotating the direction
    by the page matrix is what makes "horizontal" mean what a human sees.
    """
    dx, dy = direction
    tdx = matrix.a * dx + matrix.c * dy
    tdy = matrix.b * dx + matrix.d * dy
    return abs(tdx) >= abs(tdy)


class PdfDoc:
    """Owns a fitz.Document. Use as a context manager."""

    def __init__(self, source: bytes | str | Path):
        """Open from bytes, or from a path.

        A path is the cheap route for a large set: PyMuPDF reads pages from
        disk as they are needed, so memory stays roughly flat. Handing it bytes
        means the whole file sits in memory twice -- ours and its copy -- which
        is around a gigabyte for a 500 MB drawing set before a page is read.
        """
        doc = None
        try:
            if isinstance(source, (str, Path)):
                # Check the signature ourselves first. On a file it cannot
                # parse, PyMuPDF raises *after* taking a handle and does not
                # release it, and Windows will not delete an open file -- so a
                # rejected upload could not be cleaned up.
                _require_pdf_signature(Path(source))
                doc = fitz.open(str(source))
                self.size_bytes = Path(source).stat().st_size
            else:
                doc = fitz.open(stream=source, filetype="pdf")
                self.size_bytes = len(source)
            if doc.page_count == 0:
                raise NotAPdfError("PDF contains no pages")
        except Exception as exc:  # noqa: BLE001 - any failure means unreadable
            # Release the handle before propagating. Windows will not delete a
            # file that is still open, so a half-opened temp upload would leave
            # the caller unable to clean it up.
            if doc is not None:
                try:
                    doc.close()
                except Exception:  # noqa: BLE001 - close must never mask this
                    pass
            raise exc if isinstance(exc, NotAPdfError) else NotAPdfError(str(exc))
        self.doc = doc
        self._text_cache: dict[int, list[TextItem]] = {}
        self._drawing_page: int = -1
        self._drawing_cache: list | None = None
        self._drawing_index: dict | None = None

    def __enter__(self) -> PdfDoc:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.doc.close()
        except Exception:  # noqa: BLE001 - close must never raise
            pass

    @property
    def page_count(self) -> int:
        return self.doc.page_count

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)

    def text_items(self, page_index: int) -> list[TextItem]:
        """Non-empty spans with exact bboxes, in display space.

        Only the most recent pages are cached. The finder touches every page but
        the extractor revisits only the handful it picked, so keeping all of them
        costs a lot of memory on a 100-page set to serve one or two hits.
        """
        cached = self._text_cache.get(page_index)
        if cached is not None:
            return cached
        page = self.doc[page_index]
        matrix = page.rotation_matrix
        raw = page.get_text("dict", flags=_TEXT_FLAGS)
        items: list[TextItem] = []
        # CAD exports routinely draw the same run twice at the same spot. Left
        # in, every such cell reads "VALUE VALUE" once the cell text is joined.
        seen: set[tuple[int, int, str]] = set()
        for block in raw["blocks"]:
            if block.get("type") != 0:  # 0 = text, 1 = image
                continue
            for line in block["lines"]:
                horizontal = _is_horizontal(line.get("dir", (1.0, 0.0)), matrix)
                for span in line["spans"]:
                    text = span["text"].strip()
                    if not text:
                        continue
                    rect = fitz.Rect(span["bbox"]) * matrix
                    fingerprint = (round(rect.x0 * 2), round(rect.y0 * 2), text)
                    if fingerprint in seen:
                        continue
                    seen.add(fingerprint)
                    items.append(
                        TextItem(rect.x0, rect.y0, rect.x1, rect.y1, text,
                                 span.get("size", 0.0), horizontal)
                    )
        if len(self._text_cache) >= _TEXT_CACHE_PAGES:
            self._text_cache.pop(next(iter(self._text_cache)))
        self._text_cache[page_index] = items
        return items

    def _drawings(self, page_index: int) -> list | None:
        """The page's vector paths, parsed once.

        Finding a door's swing asks for the segments in a small window, and a
        sheet has forty doors on it -- so the same page was being parsed forty
        times over. On one 36-door plan that was 84 seconds of work to answer a
        question worth about two. Only the most recent page is kept, because
        the callers work through a sheet at a time and the parsed form of a
        busy drawing is large.
        """
        if self._drawing_page == page_index:
            return self._drawing_cache
        try:
            paths = self.doc[page_index].get_drawings()
        except Exception:  # noqa: BLE001 - malformed content streams happen
            paths = None
        self._drawing_page, self._drawing_cache = page_index, paths
        self._drawing_index = None
        return paths

    def _path_index(self, page_index: int, paths: list) -> dict:
        """Which paths lie in which part of the sheet, built once per page.

        Caching the parsed paths stopped the page being re-parsed forty times.
        It did not stop each of those forty questions *walking* all of them: a
        door swing is looked for in a window about 0.7% of the sheet, and
        answering it meant building a rotated rectangle for every one of 65,415
        paths to reject 65,198 of them. One such query costs 0.29 s and returns
        217 segments, so 99.3% of the work was thrown away -- and the arc pass
        asks roughly 1,500 of them across a set's floor plans.

        So: bucket the paths into a coarse grid of display-space cells, once.
        A window then visits the handful of cells it covers instead of the
        whole sheet. Nothing about the answer changes -- the same paths are
        tested by the same rectangle test, in the same order -- only the ones
        that could never have matched are skipped.

        Paths whose rectangle cannot be read go in `loose` and are always
        visited, because a path we cannot place is not a path we may drop.
        """
        if self._drawing_index is not None:
            return self._drawing_index

        matrix = self.doc[page_index].rotation_matrix
        cells: dict[tuple[int, int], list[int]] = {}
        loose: list[int] = []
        boxes: list[tuple[float, float, float, float] | None] = []

        for i, path in enumerate(paths):
            try:
                r = fitz.Rect(path["rect"]) * matrix
                box = (min(r.x0, r.x1), min(r.y0, r.y1),
                       max(r.x0, r.x1), max(r.y0, r.y1))
            except (KeyError, ValueError, TypeError):
                boxes.append(None)
                loose.append(i)
                continue
            boxes.append(box)
            # A path spanning many cells is listed in each of them. Sheet
            # borders and long walls do that; they are few, and the
            # alternative -- a quadtree -- is more code for the same answer.
            for cx in range(int(box[0] // _GRID), int(box[2] // _GRID) + 1):
                for cy in range(int(box[1] // _GRID), int(box[3] // _GRID) + 1):
                    cells.setdefault((cx, cy), []).append(i)

        self._drawing_index = {"cells": cells, "loose": loose, "boxes": boxes}
        return self._drawing_index

    def _paths_near(self, page_index: int, paths: list,
                    within: tuple[float, float, float, float]) -> list[int]:
        """Indices of the paths that could touch `within`, in path order.

        Order is preserved deliberately: the arc finder builds chains out of
        segments that touch each other, and a chain built in a different order
        is a different chain.
        """
        index = self._path_index(page_index, paths)
        lo_x, lo_y, hi_x, hi_y = within
        found: set[int] = set(index["loose"])
        cells = index["cells"]
        for cx in range(int(lo_x // _GRID), int(hi_x // _GRID) + 1):
            for cy in range(int(lo_y // _GRID), int(hi_y // _GRID) + 1):
                hit = cells.get((cx, cy))
                if hit:
                    found.update(hit)
        return sorted(found)

    def bookmarks(self) -> list[tuple[str, int]]:
        """The PDF's own outline as (title, 1-indexed page).

        Drawing sets are usually bookmarked one entry per sheet, carrying the
        sheet number, its title and its page in one place. That is the whole
        table of contents the pipeline otherwise reconstructs by reading the
        title block of all 187 pages.

        Empty when the file has no outline, which happens and is not an error.
        """
        try:
            return [(str(title).strip(), int(page))
                    for _level, title, page in self.doc.get_toc()
                    if page and page > 0]
        except Exception:  # noqa: BLE001 - a malformed outline is not fatal
            return []

    def segments(self, page_index: int,
                 within: tuple[float, float, float, float] | None = None
                 ) -> list[tuple[float, float, float, float]]:
        """Every vector segment on the page, at any angle, in display space.

        `rulings()` keeps only the horizontal and vertical lines, because tables
        are made of those. A door swing is made of neither: it is an arc, drawn
        as a chain of short segments at every angle in between, and finding one
        needs all of them.

        `within` bounds the search to a rectangle in display points. That matters
        for more than speed: a sheet carries tens of thousands of segments, and
        the only reason arcs can be found at all is that something else has
        already said roughly where to look.
        """
        page = self.doc[page_index]
        matrix = page.rotation_matrix
        out: list[tuple[float, float, float, float]] = []

        def add(x0: float, y0: float, x1: float, y1: float) -> None:
            a = fitz.Point(x0, y0) * matrix
            b = fitz.Point(x1, y1) * matrix
            if a.x == b.x and a.y == b.y:
                return
            if within is not None:
                lo_x, lo_y, hi_x, hi_y = within
                if (max(a.x, b.x) < lo_x or min(a.x, b.x) > hi_x
                        or max(a.y, b.y) < lo_y or min(a.y, b.y) > hi_y):
                    return
            out.append((a.x, a.y, b.x, b.y))

        drawings = self._drawings(page_index)
        if drawings is None:
            return []

        clip = fitz.Rect(*within) if within else None
        # Only the paths whose cell the window touches. Everything else could
        # not have passed the rectangle test below, so skipping it changes
        # nothing but the time taken -- see _path_index.
        if within is not None and drawings:
            candidates = (drawings[i] for i in
                          self._paths_near(page_index, drawings, within))
        else:
            candidates = drawings
        for path in candidates:
            # Whole paths can be skipped on their bounding box; most of a sheet
            # is nowhere near any given door.
            #
            # The rect has to be rotated first. `within` is in display space --
            # it comes from a door tag's position, and text is reported rotated
            # -- while `path["rect"]` is raw PDF space. On a /Rotate 270 sheet
            # the two do not overlap at all, so this test rejected every path on
            # the page and the arc finder saw an empty drawing. One 85-door set
            # is rotated on every sheet and reported 0 swings because of it.
            if clip is not None:
                try:
                    if not (fitz.Rect(path["rect"]) * matrix).intersects(clip):
                        continue
                except (KeyError, ValueError):
                    pass
            for item in path["items"]:
                kind = item[0]
                if kind == "l":
                    a, b = item[1], item[2]
                    add(a.x, a.y, b.x, b.y)
                elif kind == "c":
                    # Walk the curve, do not take its chord.
                    #
                    # The chord was "good enough" on the assumption that CAD
                    # exports arcs as chains of short straight lines. Plenty do
                    # not: one set draws every door swing as a single bezier,
                    # and a single bezier reduced to a chord is one straight
                    # line -- so the arc finder, which needs a chain of at
                    # least four touching pieces to fit a circle to, saw
                    # nothing at all. That set reported 2 swings across 144
                    # doors while its drawings show an arc at almost every one.
                    #
                    # Flattening costs a handful of points per curve and makes
                    # those arcs measurable exactly like any other.
                    p0, p1, p2, p3 = item[1], item[2], item[3], item[4]
                    previous = (p0.x, p0.y)
                    for step in range(1, _BEZIER_STEPS + 1):
                        t = step / _BEZIER_STEPS
                        s = 1.0 - t
                        px = (s * s * s * p0.x + 3 * s * s * t * p1.x
                              + 3 * s * t * t * p2.x + t * t * t * p3.x)
                        py = (s * s * s * p0.y + 3 * s * s * t * p1.y
                              + 3 * s * t * t * p2.y + t * t * t * p3.y)
                        add(previous[0], previous[1], px, py)
                        previous = (px, py)
                elif kind == "re":
                    r = item[1]
                    add(r.x0, r.y0, r.x1, r.y0)
                    add(r.x0, r.y1, r.x1, r.y1)
                    add(r.x0, r.y0, r.x0, r.y1)
                    add(r.x1, r.y0, r.x1, r.y1)
        return out

    def rulings(self, page_index: int) -> Rulings:
        """Every vector line on the page, split into vertical and horizontal.

        Table rulings become a query rather than a heuristic. `pdfjs-dist` cannot
        do this -- it is why the TypeScript pipeline samples pixels instead.
        """
        page = self.doc[page_index]
        matrix = page.rotation_matrix
        vertical: list[Segment] = []
        horizontal: list[Segment] = []

        def add(x0: float, y0: float, x1: float, y1: float) -> None:
            # Same display-space transform as the text, or on a rotated page the
            # rulings and the text they bound would disagree about which way is up.
            a = fitz.Point(x0, y0) * matrix
            b = fitz.Point(x1, y1) * matrix
            x0, y0, x1, y1 = a.x, a.y, b.x, b.y
            dx, dy = abs(x1 - x0), abs(y1 - y0)
            if dx < _STRAIGHT_TOL and dy >= _MIN_SEG_LEN:
                vertical.append(Segment((x0 + x1) / 2, min(y0, y1), max(y0, y1)))
            elif dy < _STRAIGHT_TOL and dx >= _MIN_SEG_LEN:
                horizontal.append(Segment((y0 + y1) / 2, min(x0, x1), max(x0, x1)))

        drawings = self._drawings(page_index)
        if drawings is None:
            return Rulings([], [])

        for path in drawings:
            for item in path["items"]:
                kind = item[0]
                if kind == "l":
                    a, b = item[1], item[2]
                    add(a.x, a.y, b.x, b.y)
                elif kind == "re":
                    r = item[1]
                    # A rect draws four rulings; thin rects are lines themselves.
                    add(r.x0, r.y0, r.x1, r.y0)
                    add(r.x0, r.y1, r.x1, r.y1)
                    add(r.x0, r.y0, r.x0, r.y1)
                    add(r.x1, r.y0, r.x1, r.y1)
                elif kind == "qu":
                    q = item[1]
                    add(q.ul.x, q.ul.y, q.ur.x, q.ur.y)
                    add(q.ll.x, q.ll.y, q.lr.x, q.lr.y)
                    add(q.ul.x, q.ul.y, q.ll.x, q.ll.y)
                    add(q.ur.x, q.ur.y, q.lr.x, q.lr.y)

        return Rulings(vertical, horizontal)

    def render_png(
        self, page_index: int, dpi: int = 200, clip: tuple[float, float, float, float] | None = None
    ) -> bytes:
        page = self.doc[page_index]
        rect = fitz.Rect(*clip) if clip else None
        return page.get_pixmap(dpi=dpi, clip=rect).tobytes("png")

    def has_raster(self, page_index: int) -> bool:
        """Does this page carry a bitmap? A page with no text and an image on it
        is a scan; one with neither is simply blank and not worth rendering."""
        try:
            return bool(self.doc[page_index].get_images(full=False))
        except Exception:  # noqa: BLE001 - a broken xref must not be fatal
            return False

    def page_size(self, page_index: int) -> tuple[float, float]:
        r = self.doc[page_index].rect
        return r.width, r.height
