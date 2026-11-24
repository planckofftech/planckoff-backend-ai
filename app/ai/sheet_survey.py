"""Where a plan sheet's floor plan is, and the scale it is drawn at.

Asked of a vision model, once per sheet, from a picture of the whole sheet.

The door pass has to know two things before it sends a tile: which part of
the sheet to look at, and how big a door is drawn there -- the zoom is chosen
from that. Both used to come from the door numbers printed on the sheet as
text. A scanned sheet, and one whose lettering is drawn as lines, has none:

  - MAVANO's plans carry no text at all, and Christian Brothers' A-2 carries
    one stray "1". The whole sheet, or an 81 x 90 pt patch around the "1",
    was sent -- schedules, notes and title block included.
  - Measuring arcs off the drawing instead found the sheet's column grid
    bubbles, not its doors: 14 pt on MAVANO, where a 3'-0" door at the
    sheet's 3/16" scale is 40 pt. The zoom went three times too far and one
    sheet needed 972 tiles.

The printed scale is exact where any measurement is a guess: a door of the
schedule's width, at 3/16" to the foot, is that many points wide. And a model
reads "SCALE: 3/16" = 1'-0"" and points at a floor plan far more reliably
than it finds a door.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass

from app.ai.client import AiUnavailableError, get_client
from app.ai.pricing import cost_usd
from app.ai.response_parser import _strip_fences
from app.core.pdf_doc import PdfDoc

log = logging.getLogger(__name__)

# Long edge of the picture sent. Enough for a title like FIRST FLOOR PLAN and
# the scale beneath it to be read on a 36 x 24 in sheet; more only costs.
_SURVEY_PIXELS = 3000
_MAX_OUTPUT_TOKENS = 2000

_PROMPT = (
    "This image is one sheet of an architectural drawing set. Find each FLOOR "
    "PLAN drawn on it -- a plan of a storey showing walls and doors. Not "
    "enlarged plans of a single room, not elevations, sections, details, "
    "schedules, notes, legends, key plans of the site or the title block.\n\n"
    "For each floor plan give:\n"
    "- box: the rectangle the plan's drawing occupies (its walls, doors and "
    "dimensions, not its title), as whole numbers from 0 to 1000 measured "
    "from the top-left corner of the image: x across, y down.\n"
    "- scale: the drawing scale printed for that plan, copied exactly as "
    "printed, e.g. 1/8\" = 1'-0\" or 1:100. Empty if none is printed for it.\n\n"
    "If the sheet has no floor plan, return an empty list. Never guess.\n\n"
    'Return JSON: {"plans": [{"box": {"x0": 0, "y0": 0, "x1": 1000, '
    '"y1": 1000}, "scale": "..."}]}'
)

# 3/16" = 1'-0"   1 1/2" = 1'-0"   1-1/2"=1'   .25" = 1'
_IMPERIAL = re.compile(
    r"(?:(\d+)\s*[- ]\s*)?(\d+(?:\.\d+)?|\.\d+)(?:\s*/\s*(\d+))?\s*(?:\"|''|”|IN\b|INCH)"
    r"\s*=\s*1\s*(?:'|’|FT\b|FOOT)", re.IGNORECASE)
# 1:100   1 : 50
_RATIO = re.compile(r"\b1\s*:\s*(\d+(?:\.\d+)?)\b")


def inches_per_foot(scale: str) -> float | None:
    """How many inches of paper one foot of building takes, from a scale as
    printed. None when it does not read as a scale."""
    text = (scale or "").strip()
    if not text:
        return None
    match = _IMPERIAL.search(text)
    if match:
        whole, num, den = match.groups()
        value = float(num) / float(den) if den else float(num)
        if whole:
            value += float(whole)
        return value if 0 < value <= 12 else None
    match = _RATIO.search(text)
    if match:
        ratio = float(match.group(1))
        return 12.0 / ratio if ratio >= 1 else None
    return None


@dataclass(slots=True)
class Survey:
    """The sheet's main floor plan, in page points, and its scale."""

    box: tuple[float, float, float, float] | None = None
    scale: str = ""
    inches_per_foot: float | None = None
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def estimated_usd(self) -> float:
        return cost_usd(self.model, self.prompt_tokens, self.completion_tokens)

    def door_pt(self, door_ft: float) -> float | None:
        """How wide a door of `door_ft` feet is drawn, in points."""
        if not self.inches_per_foot or not door_ft:
            return None
        return door_ft * self.inches_per_foot * 72


def _plans(content: str) -> list[dict]:
    try:
        parsed = json.loads(_strip_fences(content))
    except (TypeError, ValueError):
        return []
    found = parsed.get("plans") if isinstance(parsed, dict) else parsed
    return [p for p in found if isinstance(p, dict)] if isinstance(found, list) else []


def _box(entry: dict, width: float, height: float
         ) -> tuple[float, float, float, float] | None:
    box = entry.get("box")
    if not isinstance(box, dict):
        return None
    try:
        x0, y0, x1, y1 = (float(box[k]) for k in ("x0", "y0", "x1", "y1"))
    except (KeyError, TypeError, ValueError):
        return None
    x0, x1 = sorted((max(0.0, min(1000.0, x0)), max(0.0, min(1000.0, x1))))
    y0, y1 = sorted((max(0.0, min(1000.0, y0)), max(0.0, min(1000.0, y1))))
    if x1 - x0 < 20 or y1 - y0 < 20:
        return None
    return (x0 / 1000 * width, y0 / 1000 * height,
            x1 / 1000 * width, y1 / 1000 * height)


async def survey_sheet(doc: PdfDoc, page: int, model: str) -> Survey:
    """Ask `model` where this sheet's floor plans are and what scale they are
    drawn at. Several plans on one sheet come back as the box around all of
    them, at the scale most of them share; an empty Survey means the model
    found none or could not be asked."""
    survey = Survey(model=model)
    width, height = doc.page_size(page - 1)
    dpi = max(36, int(_SURVEY_PIXELS / (max(width, height) / 72)))
    png = doc.render_png(page - 1, dpi=dpi)
    try:
        client = get_client()
        response = await client.chat.completions.create(
            model=model,
            temperature=0,
            max_tokens=_MAX_OUTPUT_TOKENS,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": [
                {"type": "text", "text": _PROMPT},
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(png).decode()}},
            ]}],
        )
    except AiUnavailableError as exc:
        log.warning("sheet_survey page %s: %s", page, exc)
        return survey
    except Exception as exc:  # noqa: BLE001 - a failed survey falls back, it does not fail the pass
        log.warning("sheet_survey page %s: %s could not be asked: %s", page, model, exc)
        return survey

    usage = getattr(response, "usage", None)
    if usage:
        survey.prompt_tokens = usage.prompt_tokens or 0
        survey.completion_tokens = usage.completion_tokens or 0
    plans = _plans(response.choices[0].message.content or "")

    boxes = [b for b in (_box(p, width, height) for p in plans) if b]
    if boxes:
        survey.box = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                      max(b[2] for b in boxes), max(b[3] for b in boxes))
    scales = [(str(p.get("scale") or "").strip(), inches_per_foot(str(p.get("scale") or "")))
              for p in plans]
    scales = [(s, v) for s, v in scales if v]
    if scales:
        values = [v for _s, v in scales]
        common = max(set(values), key=values.count)
        survey.scale = next(s for s, v in scales if v == common)
        survey.inches_per_foot = common

    log.info("sheet_survey model=%s page=%s plans=%d box=%s scale=%r ~$%.4f",
             model, page, len(plans),
             tuple(round(v) for v in survey.box) if survey.box else None,
             survey.scale, survey.estimated_usd)
    return survey
