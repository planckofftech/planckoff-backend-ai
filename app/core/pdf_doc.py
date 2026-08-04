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

        try:
            drawings = page.get_drawings()
        except Exception:  # noqa: BLE001 - malformed content streams happen
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
