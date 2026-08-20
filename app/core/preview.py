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
    table_top,
)

_OUTLINE = (200, 25, 25)
_BORDER_WIDTH = 4
_LABEL_PAD = 4
# One door's tag is a dozen points wide on a 36-inch sheet. Drawn faithfully it
# is a red speck; these give every mark a findable minimum size and a thinner
# line, so 40 of them on one plan read as a pattern rather than a smear.
_MIN_MARK = 26.0
_MARK_WIDTH = 2
# Margin left around the doors when cropping to them, and the width in pixels
# the crop aims for -- enough to read room names, capped so one sheet is not a
# 30 MB download.
_CLIP_PAD = 0.06
_MIN_CLIP_PAD = 24.0
_MARK_PIXELS = 2600
_MAX_MARK_DPI = 260
# Points to pixels: PDF user space is 72 dpi.
_PDF_DPI = 72.0


def _table_bottom(grid: TableGrid, doc: PdfDoc, page_index: int) -> float:
    """Where the table ends -- the last ruled line that still has rows above it.

    Taking the last ruled line outright drew the box past the schedule and
    around whatever was ruled beneath it: on one sheet it swallowed the GLAZING
    TYPES and DEMOUNTABLE REQUIREMENTS tables below. Those bands are empty as
    far as *this* grid's columns are concerned, so the last band holding text is
    the real edge.
    """
    inside = [
        item for item in doc.text_items(page_index)
        if item.horizontal and item.cy > grid.header_bottom
        and grid.column_of(item.x0) is not None
    ]
    lowest = max((i.y1 for i in inside), default=None)

    if grid.row_bounds:
        if lowest is None:
            return grid.row_bounds[-1]
        # The first boundary at or below the lowest row of text.
        return next((b for b in grid.row_bounds if b >= lowest - 1),
                    grid.row_bounds[-1])
    return lowest if lowest is not None else grid.header_bottom + 40.0


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


def _marks_clip(marks: list[tuple[float, float, float, float, str]],
                width: float, height: float) -> tuple[float, float, float, float]:
    """The part of the sheet the doors actually occupy, with room to breathe.

    A drawing sheet is mostly not the plan: on one real sheet the floor plan is
    a quarter of the page and the rest is wall-type details and notes. Rendering
    the whole sheet made 36 correctly-found doors a cluster of specks in one
    corner. Cropping to the doors is the difference between a picture that
    proves the answer and one that merely contains it.
    """
    x0 = min(m[0] for m in marks) * width
    y0 = min(m[1] for m in marks) * height
    x1 = max(m[2] for m in marks) * width
    y1 = max(m[3] for m in marks) * height
    pad_x = max((x1 - x0) * _CLIP_PAD, _MIN_CLIP_PAD)
    pad_y = max((y1 - y0) * _CLIP_PAD, _MIN_CLIP_PAD)
    return (max(0.0, x0 - pad_x), max(0.0, y0 - pad_y),
            min(width, x1 + pad_x), min(height, y1 + pad_y))


def _draw_marks(draw: ImageDraw.ImageDraw, image: Image.Image,
                marks: list[tuple[float, float, float, float, str]],
                clip: tuple[float, float, float, float],
                width: float, height: float,
                drawn_out: list[tuple[float, float, float, float]] | None = None
                ) -> None:
    """Outline many things at once, each with its own label.

    Coordinates arrive as fractions of the whole page but the image is a crop
    of it, so they are mapped through the clip. A door tag is a dozen points
    across on a 36-inch drawing, so each box is grown to a legible minimum --
    a faithful box would be a red speck nobody can find.

    `drawn_out` collects the rectangles as they were actually drawn, back in
    page fractions. The caller needs them because it lays a clickable overlay
    on this image: built from the original fractions instead, the click target
    for a small door sits inside and off-centre of the red box a person can
    see, and clicking the box does nothing.
    """
    span_x = max(clip[2] - clip[0], 1e-6)
    span_y = max(clip[3] - clip[1], 1e-6)
    font = _font(max(11, int(image.width / 90)))

    for x0, y0, x1, y1, label in marks:
        left = (x0 * width - clip[0]) / span_x * image.width
        right = (x1 * width - clip[0]) / span_x * image.width
        top = (y0 * height - clip[1]) / span_y * image.height
        bottom = (y1 * height - clip[1]) / span_y * image.height
        grow_x = max(0.0, (_MIN_MARK - (right - left)) / 2)
        grow_y = max(0.0, (_MIN_MARK - (bottom - top)) / 2)
        left, right = left - grow_x, right + grow_x
        top, bottom = top - grow_y, bottom + grow_y
        draw.rectangle((left, top, right, bottom), outline=_OUTLINE,
                       width=_MARK_WIDTH)
        if drawn_out is not None:
            drawn_out.append((
                (left / image.width * span_x + clip[0]) / width,
                (top / image.height * span_y + clip[1]) / height,
                (right / image.width * span_x + clip[0]) / width,
                (bottom / image.height * span_y + clip[1]) / height,
            ))
        if not label:
            continue
        text_box = draw.textbbox((0, 0), label, font=font)
        text_w = text_box[2] - text_box[0]
        text_h = text_box[3] - text_box[1]
        x = min(max(0, left), image.width - text_w - 2)
        y = top - text_h - 5
        if y < 0:
            y = bottom + 2
        draw.rectangle((x, y, x + text_w + 4, y + text_h + 4), fill=_OUTLINE)
        draw.text((x + 2, y + 2), label, fill=(255, 255, 255), font=font)


# How many outlines one sheet may carry before the picture is worse than the
# thing it is showing.
_MAX_TABLES_DRAWN = 4
# Two outlines whose corners are within this many points are the same table
# reached from two header bands.
_SAME_TABLE = 12.0


def _bounds(grid, doc: PdfDoc, page_index: int) -> tuple[float, float, float, float]:
    """The whole table in PDF points: caption, headings and rows."""
    items = doc.text_items(page_index)
    rulings = doc.rulings(page_index)
    top = min(grid.header_top, grid.header_bottom, table_top(grid, items, rulings))
    title_top, _title = _title_band(grid, doc, page_index, rulings)
    if title_top is not None:
        top = min(top, title_top)
    return (grid.left, top, grid.right, _table_bottom(grid, doc, page_index))


def _other_tables(doc: PdfDoc, page_index: int, candidate: PageCandidate,
                  drawn) -> list[tuple[tuple[float, float, float, float], str]]:
    """Schedules on this page other than the one the candidate points at."""
    from app.core import page_finder

    items = doc.text_items(page_index)
    rulings = doc.rulings(page_index)
    seen = [_bounds(drawn, doc, page_index)]
    out: list[tuple[tuple[float, float, float, float], str]] = []
    for band in page_finder.header_bands(items, candidate.page):
        if len(out) + 1 >= _MAX_TABLES_DRAWN:
            break
        try:
            grid, _headers = locate_table(items, rulings, band.header_y,
                                          band.tag_x)
            box = _bounds(grid, doc, page_index)
        except (TableNotFoundError, IndexError, ValueError):
            continue
        if any(all(abs(a - b) <= _SAME_TABLE for a, b in zip(box, had))
               for had in seen):
            continue
        seen.append(box)
        _title_top, title = _title_band(grid, doc, page_index, rulings)
        out.append((box, title or "DOOR SCHEDULE"))
    return out


def render_preview(doc: PdfDoc, candidate: PageCandidate, *, dpi: int = 110,
                   located: bool = True,
                   box: tuple[float, float, float, float] | None = None,
                   box_label: str = "",
                   marks: list[tuple[float, float, float, float, str]] | None = None,
                   clip_out: list[float] | None = None,
                   drawn_out: list[tuple[float, float, float, float]] | None = None,
                   draw: bool = True, whole: bool = False) -> bytes:
    """PNG of the page, with the door schedule boxed when it was actually found.

    `located` must be False when the page did not pass the structural gates. On
    such a page the locator will still latch onto *some* ruled block -- on one
    real sheet it boxed the MATERIAL KEY legend and labelled it DOOR SCHEDULE.
    Drawing that is worse than drawing nothing: it asserts a result the finder
    never reached. The page is still shown, because it is what the vision tier
    read.

    `box` overrides both, given as fractions of the page. That is how an AI-read
    page gets an outline at all: the model returns text, never geometry, so the
    rectangle is worked out afterwards and handed back in here.
    """
    page_index = candidate.page - 1

    if marks:
        # Crop to the doors first, then render. A smaller area also affords a
        # higher dpi for the same number of pixels, so the plan is legible.
        width, height = doc.page_size(page_index)
        # `whole` gives back the entire sheet. Cropping to the doors makes them
        # legible, but it also decides for the reader what is worth looking at:
        # where a sheet's doors sit in one corner the crop throws the rest of
        # the building away, and a door that was missed is in the part that was
        # cut off. You cannot check for absences in a picture cropped to the
        # things that were found.
        clip = ((0.0, 0.0, width, height) if whole
                else _marks_clip(marks, width, height))
        if clip_out is not None:
            # The caller needs this to lay anything over the image -- without it
            # a page fraction cannot be turned into a position in the crop.
            clip_out.extend([clip[0] / width, clip[1] / height,
                             clip[2] / width, clip[3] / height])
        span = max(clip[2] - clip[0], 1.0)
        crop_dpi = min(_MAX_MARK_DPI, max(dpi, int(_MARK_PIXELS / span * _PDF_DPI)))
        image = Image.open(io.BytesIO(
            doc.render_png(page_index, dpi=crop_dpi, clip=clip))).convert("RGB")
        # Every door on the sheet at once. No grid is measured and no single
        # label is drawn: the point of this view is the pattern -- where the
        # doors are, and where a stretch of building has none.
        # `draw=False` still crops to the doors and still reports where it
        # cropped -- the caller wants the plan itself, to lay its own overlay
        # over. A box is the crudest thing that can be drawn on a door; the
        # arc we measured is the door.
        if draw:
            _draw_marks(ImageDraw.Draw(image), image, marks, clip, width,
                        height, drawn_out)
        out = io.BytesIO()
        image.save(out, format="PNG", optimize=True)
        return out.getvalue()

    image = Image.open(io.BytesIO(doc.render_png(page_index, dpi=dpi))).convert("RGB")
    scale = dpi / _PDF_DPI
    draw = ImageDraw.Draw(image)

    grid = None
    others: list[tuple[tuple[float, float, float, float], str]] = []
    if located:
        try:
            grid, _headers = locate_table(doc.text_items(page_index),
                                          doc.rulings(page_index),
                                          candidate.header_y, candidate.tag_x)
        except (TableNotFoundError, IndexError, ValueError):
            grid = None
        # Every other schedule on the same sheet, outlined too.
        #
        # A sheet routinely carries more than one: a door schedule continued
        # below its own first half, or two of them side by side. Both halves are
        # read and both reach the rows -- 49 doors plus 36, 15 plus 9 -- but
        # only the one this candidate points at was ever drawn on, so the
        # picture said we had found half of what we had found.
        if grid is not None:
            others = _other_tables(doc, page_index, candidate, grid)

    if box is not None:
        # Supplied by the caller because the AI tier read this page: the grid
        # could not be measured, so the outline comes from where the extracted
        # values were found instead. Drawn in the same red as a measured one,
        # because it means the same thing -- this is what was read.
        x0, y0, x1, y1 = box
        draw.rectangle(
            (x0 * image.width, y0 * image.height,
             x1 * image.width, y1 * image.height),
            outline=_OUTLINE, width=_BORDER_WIDTH,
        )
        # Never "DOOR SCHEDULE" by default. This rectangle is wherever the
        # caller says a table was read, and captioning it with the thing we were
        # looking for turns the picture into a claim: one sheet's LIGHT FIXTURE
        # SCHEDULE was outlined in red and labelled DOOR SCHEDULE, which read as
        # the finder insisting on an answer everyone could see was wrong.
        label = box_label or "TABLE READ HERE"
        box = (x0 * image.width, y0 * image.height, 0, 0)
    elif grid is not None:
        top = min(grid.header_top, grid.header_bottom,
                  table_top(grid, doc.text_items(page_index),
                            doc.rulings(page_index)))
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

    # The rest of the sheet's schedules, drawn the same way, so a continued or
    # side-by-side half is not left looking like something we missed.
    for (bx0, by0, bx1, by1), name in others:
        draw.rectangle((bx0 * scale, by0 * scale, bx1 * scale, by1 * scale),
                       outline=_OUTLINE, width=_BORDER_WIDTH)
        size = draw.textbbox((0, 0), name, font=font)
        width_, height_ = size[2] - size[0], size[3] - size[1]
        lx = min(bx0 * scale, max(0, image.width - width_ - _LABEL_PAD))
        ly = by0 * scale - height_ - _LABEL_PAD * 2
        if ly < 0:
            ly = by0 * scale + _LABEL_PAD
        draw.rectangle(
            (lx, ly, lx + width_ + _LABEL_PAD * 2, ly + height_ + _LABEL_PAD * 2),
            fill=_OUTLINE,
        )
        draw.text((lx + _LABEL_PAD, ly + _LABEL_PAD), name,
                  fill=(255, 255, 255), font=font)

    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()
