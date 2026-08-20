"""Move a roughly-right box onto the door it was meant to be on.

A vision model is good at *"a door is around here"* and poor at *"here exactly"*.
Measured on one real sheet: 89% of doors were found, but the median box sat
36 pt from its door -- on a door about 36 pt wide. A box a full door-width off
has essentially no overlap with the thing it names, which is the difference
between a picture that proves the answer and one that merely gestures at it.

Published benchmarks say the same in their own way: models score in the high
eighties on bedrooms, which are large and carry a text label, and around forty
on doors, which are small, identical and numerous. Localising a door is the
hard case, and no prompt fixes it.

Pixels can. Inside a small patch the door is the only ink, so:

    grow the model's box  ->  render just that patch  ->  find the dark pixels
    ->  move the box onto them  ->  pad it back out

The padding is not decoration. A door is the panel *and* its arc; the arc is the
thinnest line in the group and the first thing a tight box clips. A box that
cuts the arc off no longer shows why we called this a door.

No model call, no new dependency, milliseconds. It is the deterministic half of
"model for what, pixels for where".
"""

from __future__ import annotations

import io
import logging

from PIL import Image

from app.core.pdf_doc import PdfDoc

log = logging.getLogger(__name__)

# How far out to look around the model's box before hunting for ink. The box is
# routinely a door-width off, so the search has to be wider than the error.
_SEARCH_GROW = 1.6
# Anything darker than this is ink. Drawings are black on white; the threshold
# only has to separate line work from paper and from the pale blue overlays.
_INK_LEVEL = 160
# A column or row of the patch counts as ink when this fraction of it is dark.
# One stray pixel is dirt; a line is a run.
_INK_FRACTION = 0.02
# Margin added after snapping, as a fraction of the box, with a floor in points
# so a small door still gets room for its arc.
_PAD = 0.15
_MIN_PAD_PT = 4.0
# Refinement must never make things worse. Outside this band of the expected
# door size, the ink we found is the wall or a fragment, not the door.
_MIN_SCALE = 0.35
_MAX_SCALE = 2.2
# Rendering resolution for the patch. High enough to see a thin arc.
_PATCH_DPI = 300


def _ink_bounds(image: Image.Image) -> tuple[int, int, int, int] | None:
    """The box around the dark pixels, or None if the patch is blank.

    Works on row and column profiles rather than per-pixel bounds: a single
    speck of dirt would otherwise set the edge, and drawings are full of specks.
    """
    grey = image.convert("L")
    width, height = grey.size
    if width < 3 or height < 3:
        return None
    pixels = grey.load()

    columns = [0] * width
    rows = [0] * height
    for y in range(height):
        for x in range(width):
            if pixels[x, y] < _INK_LEVEL:
                columns[x] += 1
                rows[y] += 1

    def span(profile: list[int], across: int) -> tuple[int, int] | None:
        need = max(1, int(across * _INK_FRACTION))
        hits = [i for i, v in enumerate(profile) if v >= need]
        return (hits[0], hits[-1]) if hits else None

    horizontal = span(columns, height)
    vertical = span(rows, width)
    if horizontal is None or vertical is None:
        return None
    return horizontal[0], vertical[0], horizontal[1], vertical[1]


def refine(doc: PdfDoc, page: int, box: tuple[float, float, float, float],
           door_pt: float) -> tuple[tuple[float, float, float, float], str]:
    """Snap one box (page fractions) onto the door, and pad it.

    Returns the box and a one-word reason, so a run can report how often
    refinement helped rather than claiming it always does.
    """
    width, height = doc.page_size(page - 1)
    x0, y0, x1, y1 = (box[0] * width, box[1] * height,
                      box[2] * width, box[3] * height)
    if x1 <= x0 or y1 <= y0:
        return box, "degenerate"

    # Search a patch wider than the error we are correcting for.
    grow_x = (x1 - x0) * _SEARCH_GROW / 2
    grow_y = (y1 - y0) * _SEARCH_GROW / 2
    patch = (max(0.0, x0 - grow_x), max(0.0, y0 - grow_y),
             min(width, x1 + grow_x), min(height, y1 + grow_y))
    if patch[2] - patch[0] < 2 or patch[3] - patch[1] < 2:
        return box, "patch-too-small"

    try:
        image = Image.open(io.BytesIO(
            doc.render_png(page - 1, dpi=_PATCH_DPI, clip=patch)))
    except Exception as exc:  # noqa: BLE001 - a bad patch must not kill a scan
        log.warning("box_refine: could not render patch on page %s: %s", page, exc)
        return box, "render-failed"

    bounds = _ink_bounds(image)
    if bounds is None:
        return box, "no-ink"

    scale_x = (patch[2] - patch[0]) / image.width
    scale_y = (patch[3] - patch[1]) / image.height
    nx0 = patch[0] + bounds[0] * scale_x
    ny0 = patch[1] + bounds[1] * scale_y
    nx1 = patch[0] + (bounds[2] + 1) * scale_x
    ny1 = patch[1] + (bounds[3] + 1) * scale_y

    # Clamps. Ink far bigger than a door is the wall or a dimension string; ink
    # far smaller is a fragment. Either way the model's own box is the safer
    # answer, and saying so is better than silently returning something wrong.
    span = max(nx1 - nx0, ny1 - ny0)
    if span > door_pt * _MAX_SCALE:
        return box, "too-big"
    if span < door_pt * _MIN_SCALE:
        return box, "too-small"

    pad_x = max((nx1 - nx0) * _PAD, _MIN_PAD_PT)
    pad_y = max((ny1 - ny0) * _PAD, _MIN_PAD_PT)
    return ((max(0.0, nx0 - pad_x) / width, max(0.0, ny0 - pad_y) / height,
             min(width, nx1 + pad_x) / width, min(height, ny1 + pad_y) / height),
            "snapped")
