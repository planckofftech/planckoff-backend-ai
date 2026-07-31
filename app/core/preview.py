"""Render the candidate page with the detected table outlined.

Visual confirmation that the service located the right region -- the fastest way
to tell "extracted 23 rows" apart from "extracted 23 rows from the wrong table".

The box is drawn onto the *rendered image*, not into the PDF. Rendering already
applies /Rotate, so image space is display space and no rotation arithmetic is
needed -- drawing into the PDF instead put the label sideways in the margin on
rotated sheets.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

from app.core.page_finder import PageCandidate
from app.core.pdf_doc import PdfDoc
from app.core.table_locator import (
    TableGrid,
    TableNotFoundError,
    locate_table,
    table_title,
)

_OUTLINE = (200, 25, 25)
_BORDER_WIDTH = 4
_LABEL_PAD = 4
# Points to pixels: PDF user space is 72 dpi.
_PDF_DPI = 72.0


def _table_bottom(grid: TableGrid, doc: PdfDoc, page_index: int) -> float:
    """Where the table ends. Ruled grids say so directly; otherwise take the
    lowest text still inside a column band."""
    if grid.row_bounds:
        return grid.row_bounds[-1]
    bottoms = [
        item.y1
        for item in doc.text_items(page_index)
        if item.horizontal
        and item.y0 > grid.header_bottom
        and grid.column_of(item.x0) is not None
    ]
    return max(bottoms) if bottoms else grid.header_bottom + 40.0


def _title_band(grid: TableGrid, doc: PdfDoc, page_index: int,
                rulings) -> tuple[float | None, str]:
    """The table's own caption. Shared with extraction so the outline and the
    table's reported title always agree."""
    return table_title(grid, doc.text_items(page_index), rulings)


def _font(size: int) -> ImageFont.ImageFont:
    """A readable label if a TrueType face is available, the bitmap default if
    not -- the container image may ship no fonts at all."""
    for name in ("arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_preview(doc: PdfDoc, candidate: PageCandidate, *, dpi: int = 110,
                   located: bool = True) -> bytes:
    """PNG of the page, with the door schedule boxed when it was actually found.

    `located` must be False when the page did not pass the structural gates. On
    such a page the locator will still latch onto *some* ruled block -- on one
    real sheet it boxed the MATERIAL KEY legend and labelled it DOOR SCHEDULE.
    Drawing that is worse than drawing nothing: it asserts a result the finder
    never reached. The page is still shown, because it is what the vision tier
    read.
    """
    page_index = candidate.page - 1
    image = Image.open(io.BytesIO(doc.render_png(page_index, dpi=dpi))).convert("RGB")
    scale = dpi / _PDF_DPI
    draw = ImageDraw.Draw(image)

    grid = None
    if located:
        try:
            grid, _headers = locate_table(doc.text_items(page_index),
                                          doc.rulings(page_index),
                                          candidate.header_y, candidate.tag_x)
        except (TableNotFoundError, IndexError, ValueError):
            grid = None

    if grid is not None:
        top = min(grid.header_top, grid.header_bottom)
        title_top, title = _title_band(grid, doc, page_index, doc.rulings(page_index))
        if title_top is not None:
            top = min(top, title_top)
        bottom = _table_bottom(grid, doc, page_index)
        draw.rectangle(
            (grid.left * scale, top * scale, grid.right * scale, bottom * scale),
            outline=_OUTLINE, width=_BORDER_WIDTH,
        )
        label = title or "DOOR SCHEDULE"
        box = (grid.left * scale, top * scale, 0, 0)
    else:
        label = f"page {candidate.page} - no schedule located; shown as read"
        box = (_LABEL_PAD, _LABEL_PAD * 2 + 14, 0, 0)
    font = _font(max(13, int(image.width / 90)))
    text_box = draw.textbbox((0, 0), label, font=font)
    text_w = text_box[2] - text_box[0]
    text_h = text_box[3] - text_box[1]

    # Sit the label above the box, or inside the top edge when there is no room.
    x = min(box[0], max(0, image.width - text_w - _LABEL_PAD))
    y = box[1] - text_h - _LABEL_PAD * 2
    if y < 0:
        y = box[1] + _LABEL_PAD
    draw.rectangle(
        (x, y, x + text_w + _LABEL_PAD * 2, y + text_h + _LABEL_PAD * 2),
        fill=_OUTLINE,
    )
    draw.text((x + _LABEL_PAD, y + _LABEL_PAD), label, fill=(255, 255, 255), font=font)

    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()
