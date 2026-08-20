"""Read a page that has no text layer, and hand back the same TextItems as one
that does.

Half of a drawing set arrives flattened: the sheets are vector line work with
every glyph converted to outlines, so a page that looks perfectly readable
carries zero text spans. On one 27-page set the first eleven sheets -- the whole
architectural block, including the door schedule -- are like this.

Everything in pipelines 1 and 2 is built on `TextItem`: the page finder scores
header words, `plan_index` reads the title block, `door_locator` searches for
door numbers. None of it needs to know where those items came from. So this is
the one seam: give it a page, get TextItems, and the rest of the service works
unchanged on a scanned set.

Deliberately not wired into `PdfDoc.text_items`. Recognising a page costs a
render and a few seconds, and the finder touches every page of a 200-page set --
so this is called for the handful of pages that are worth the money, by callers
that know which ones those are.
"""

from __future__ import annotations

import io
import logging
import math

from app.core.pdf_doc import TextItem

log = logging.getLogger(__name__)

# Enough to read a title block and a schedule's cells. Higher is better for
# door tags on a plan, which are a few points tall -- see `_TAG_DPI`.
_TITLE_DPI = 150
_TAG_DPI = 300
# Below this the recogniser is guessing. Kept low: a door number is three
# characters and scores worse than a sentence, and a missed door costs more
# than a stray word that no rule will match anyway.
_MIN_CONFIDENCE = 0.45
# All in multiples of the shorter box's own height, so a tall heading and a
# line of small print are each judged at their own scale.
_SAME_LINE = 0.6      # two boxes share a baseline
_WORD_GAP = 1.5       # ...and are near enough to be one phrase
_LINE_GAP = 1.2       # one caption line sits under another
_STACK_OVERLAP = 0.35  # ...and they line up across the page
# A box this short says nothing about which way its text runs.
_EITHER_WAY = 2

_reader = None


class OcrUnavailable(RuntimeError):
    """Raised when no recogniser is installed. Callers fall back to text."""


def available() -> bool:
    try:
        _engine()
    except OcrUnavailable:
        return False
    return True


def _engine():
    """The recogniser, loaded once.

    Imported lazily so the service starts, and every text-bearing set keeps
    working, on a machine with no OCR installed at all.
    """
    global _reader
    if _reader is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise OcrUnavailable(
                "no OCR engine installed; scanned sheets cannot be read. "
                "pip install rapidocr-onnxruntime"
            ) from exc
        _reader = RapidOCR()
    return _reader


def _is_horizontal(x0: float, y0: float, x1: float, y1: float,
                   text: str) -> bool:
    """Is this box's text running across the page as displayed?

    A drawing set letters its title block sideways as a matter of course -- the
    project name up the right-hand edge -- and the rest of the service asks
    TextItem the same question of embedded text.

    Decided on the box, not on the recogniser's own corner order, which reports
    sideways text as if it were upright: one such strip came back 409 points
    tall and was then the largest "heading" on the sheet, ahead of the title.
    A run of characters set across the page is wider than it is tall; two
    characters or fewer can be either, and are left alone.
    """
    if len(text.strip()) <= _EITHER_WAY:
        return True
    return (x1 - x0) >= (y1 - y0)


def read(doc, page: int, *, clip=None, dpi: int = _TITLE_DPI) -> list[TextItem]:
    """Recognise a page, or a rectangle of one, as TextItems in PDF points.

    `clip` is in the page's own display coordinates -- the same space every
    other TextItem uses -- so a caller can read just the title block without
    doing arithmetic. Returns [] rather than raising when nothing is legible.
    """
    import fitz
    from PIL import Image
    import numpy as np

    engine = _engine()
    sheet = doc.doc[page - 1]
    window = fitz.Rect(clip) if clip is not None else sheet.rect
    pixels = sheet.get_pixmap(clip=window, dpi=dpi)
    image = np.array(
        Image.open(io.BytesIO(pixels.tobytes("png"))).convert("RGB"))

    found, _elapsed = engine(image)
    if not found:
        log.info("ocr: nothing legible on page %s", page)
        return []

    # Pixels back to points, and back into the window the caller asked for.
    scale = dpi / 72.0
    items: list[TextItem] = []
    for polygon, text, confidence in found:
        if confidence < _MIN_CONFIDENCE or not text.strip():
            continue
        xs = [p[0] for p in polygon]
        ys = [p[1] for p in polygon]
        x0 = window.x0 + min(xs) / scale
        x1 = window.x0 + max(xs) / scale
        y0 = window.y0 + min(ys) / scale
        y1 = window.y0 + max(ys) / scale
        items.append(TextItem(x0, y0, x1, y1, text.strip(), y1 - y0,
                              _is_horizontal(x0, y0, x1, y1, text)))
    log.info("ocr: page %s gave %d text item(s) at %d dpi",
             page, len(items), dpi)
    return items


def _merge(group: list[TextItem]) -> TextItem:
    ordered = sorted(group, key=lambda i: (round(i.cy, 1), i.x0))
    return TextItem(min(i.x0 for i in group), min(i.y0 for i in group),
                    max(i.x1 for i in group), max(i.y1 for i in group),
                    " ".join(i.text for i in ordered),
                    max(i.size for i in group), True)


def stitch_lines(items: list[TextItem]) -> list[TextItem]:
    """Join boxes that sit on one printed line, and only those.

    A recogniser returns what it sees, in pieces. Joining along the line is
    safe -- the pieces really are one phrase. Joining *down* the page is not:
    doing it by nearness alone welded a phone number, a firm name and a street
    address into a single 409-point item, because a tall box makes a tall
    tolerance and everything within it looks adjacent.
    """
    horizontal = sorted((i for i in items if i.horizontal),
                        key=lambda i: (i.y0, i.x0))
    out: list[TextItem] = [i for i in items if not i.horizontal]
    line: list[TextItem] = []
    for item in horizontal:
        if line:
            height = max(min(item.y1 - item.y0, line[-1].y1 - line[-1].y0), 1.0)
            same_line = abs(item.cy - line[-1].cy) <= height * _SAME_LINE
            adjacent = item.x0 - line[-1].x1 <= height * _WORD_GAP
            if not (same_line and adjacent):
                out.append(_merge(line))
                line = []
        line.append(item)
    if line:
        out.append(_merge(line))
    return out


def stack_block(items: list[TextItem]) -> list[TextItem]:
    """Join lines that sit directly under one another into one caption.

    Separate from `stitch_lines` and used only where a caption is expected: a
    sheet title is routinely set on three lines -- "Floor Plan and" /
    "Reflected" / "Ceiling Plan" -- and the rules that read it match phrases,
    so "FLOOR PLAN" has to survive as a phrase.

    Requires the lines to overlap horizontally, which is what tells a stacked
    caption from two unrelated things that happen to be near each other.
    """
    lines = sorted((i for i in items if i.horizontal), key=lambda i: i.y0)
    out: list[TextItem] = [i for i in items if not i.horizontal]

    # Grouped, not walked in order. A title block is columns of text, and the
    # lines of one caption are not always consecutive down the page -- a stray
    # mark between "Floor Plan and" and "Reflected" broke the run, and loosening
    # the tolerances could not fix it because the problem was never the gap.
    # Anything that does not belong simply starts a group of its own.
    blocks: list[list[TextItem]] = []
    for item in lines:
        for block in blocks:
            last = block[-1]
            height = max(min(item.y1 - item.y0, last.y1 - last.y0), 1.0)
            close = 0 <= item.y0 - last.y1 <= height * _LINE_GAP
            overlap = min(item.x1, last.x1) - max(item.x0, last.x0)
            share = overlap / max(min(item.x1 - item.x0,
                                      last.x1 - last.x0), 1.0)
            if close and share >= _STACK_OVERLAP:
                block.append(item)
                break
        else:
            blocks.append([item])
    out.extend(_merge(b) for b in blocks)
    return out
