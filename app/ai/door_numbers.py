"""Read each found door's number off a close picture of that door alone.

The door pass asks a model to find every door on a tile and, in the same
breath, read the number printed beside each. On a sheet whose numbers are text
that second job is done from the text instead. On one whose numbers are drawn
as lines or scanned it is all the model, and it is the weak half: on Christian
Brothers' A-2 both models found 20 doors and named 3 of the 10, reading the
split circles (door number over hardware set) and the 1/A-8 callouts around
them as anything but a door number.

One question about one door does better than forty questions about a tile.
Each door still without a number is cropped with the room around it, the door
at the centre, and the model picks the schedule number printed for it -- or
none. About a thousandth of a dollar a door.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass

from app.ai.client import AiUnavailableError, get_client
from app.ai.pricing import cost_usd
from app.ai.response_parser import _strip_fences
from app.core.pdf_doc import PdfDoc

log = logging.getLogger(__name__)

# How far round the door the picture reaches, in door widths each way. A tag
# sits within a door width or two of its door; the room beyond shows which
# door the tag belongs to when two are close.
_REACH_DOORS = 2.5
# Long edge of each picture, in pixels.
_CROP_PIXELS = 900
_MAX_OUTPUT_TOKENS = 300
_CONCURRENCY = 6

_PROMPT = (
    "This is a close crop of an architectural floor plan. The door to name "
    "is the one at the CENTRE of the image.\n\n"
    "Find the door number printed for that door. It is usually beside it, "
    "often inside a small mark -- a circle, hexagon, diamond, square or "
    "ellipse. A mark split by a line carries the door number in its TOP half "
    "and a hardware set below: give only the top. A door number is never a "
    "room number (usually boxed under a room name), a column grid bubble, or "
    "a detail or section reference such as 1/A-8 or 3/A5.1.\n\n"
    "The door schedule lists these door numbers: {numbers}. Answer with one "
    "of them only if it is printed for the centre door. If no door number is "
    "printed for it, or you cannot tell which door a number belongs to, "
    "answer \"\".\n\n"
    'Return JSON: {{"tag": "..."}}'
)


@dataclass(slots=True)
class Reading:
    model: str = ""
    asked: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def estimated_usd(self) -> float:
        return cost_usd(self.model, self.prompt_tokens, self.completion_tokens)


def _tag(content: str) -> str:
    try:
        parsed = json.loads(_strip_fences(content))
    except (TypeError, ValueError):
        return ""
    return str(parsed.get("tag", "") if isinstance(parsed, dict) else "").strip()


async def read_numbers(doc: PdfDoc, page: int,
                       centres: list[tuple[float, float]], door_pt: float,
                       numbers: list[str], model: str) -> tuple[list[str], Reading]:
    """The schedule number printed for each door centred at `centres` (page
    points), "" where none is. One small picture and one question a door."""
    reading = Reading(model=model)
    if not centres or not numbers:
        return [""] * len(centres), reading
    try:
        client = get_client()
    except AiUnavailableError as exc:
        log.warning("door_numbers page %s: %s", page, exc)
        return [""] * len(centres), reading

    width, height = doc.page_size(page - 1)
    reach = max(door_pt, 1.0) * _REACH_DOORS
    dpi = max(72, int(_CROP_PIXELS / (2 * reach / 72)))
    prompt = _PROMPT.format(numbers=", ".join(numbers))
    allowed = set(numbers)
    gate = asyncio.Semaphore(_CONCURRENCY)

    async def ask(x: float, y: float) -> str:
        clip = (max(0.0, x - reach), max(0.0, y - reach),
                min(width, x + reach), min(height, y + reach))
        png = doc.render_png(page - 1, dpi=dpi, clip=clip)
        async with gate:
            try:
                response = await client.chat.completions.create(
                    model=model, temperature=0, max_tokens=_MAX_OUTPUT_TOKENS,
                    response_format={"type": "json_object"},
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {
                            "url": "data:image/png;base64,"
                                   + base64.b64encode(png).decode()}},
                    ]}],
                )
            except Exception as exc:  # noqa: BLE001 - one door unread is not the pass failing
                log.warning("door_numbers page %s: %s could not be asked: %s",
                            page, model, exc)
                return ""
        reading.asked += 1
        usage = getattr(response, "usage", None)
        if usage:
            reading.prompt_tokens += usage.prompt_tokens or 0
            reading.completion_tokens += usage.completion_tokens or 0
        tag = _tag(response.choices[0].message.content or "")
        return tag if tag in allowed else ""

    tags = list(await asyncio.gather(*(ask(x, y) for x, y in centres)))
    log.info("door_numbers model=%s page=%s doors=%d named=%d ~$%.4f",
             model, page, len(centres), sum(1 for t in tags if t),
             reading.estimated_usd)
    return tags, reading
