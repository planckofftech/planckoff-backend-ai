"""Read a wall-type legend off the sheet with the vision model.

The deterministic reader in `core/wall_tags` handles a legend laid out as a
table: a symbol in one column, its build-up beside it. That covers rather less
than half of real sets. Measured across seventeen projects, five publish their
partition types some other way and return nothing at all:

    section drawings   King's City, Ellis. Each type drawn in section with its
                       layers labelled around it. No rows to read.
    prose on details   Denison, Polycoat. "6" STUDS AT TYPE B" written beside a
                       detail, nowhere near a legend.
    caption only       Willowbrae. The sheet says PARTITION TYPES and the
                       specification is in the drawing, not the text.

Four layouts in seventeen sets means there is a fifth, so this stops trying to
name them. The model reads the sheet the way a person does and returns the same
thing every reader returns: which symbols this set uses for its wall types.

Cost is one image per project, and only for the projects the free reader could
not manage -- the sets that already work never reach this.
"""

from __future__ import annotations

import base64
import json
import logging

from app.ai.client import AiUnavailableError, get_client
from app.config import get_settings

log = logging.getLogger(__name__)

# What the sheet is rendered at. Legends are dense with small type, and the
# symbols are the point -- at 150 the characters inside a diamond are legible
# without the image growing large enough to be slow or expensive.
_DPI = 150

_SYSTEM = (
    "You read construction drawings. You are given one sheet that defines a "
    "building's wall or partition types. Return only what the sheet states."
)

_PROMPT = """This sheet defines the wall (partition) types for a building.

Return JSON: {"types": [{"symbol": "...", "description": "..."}]}

- `symbol` is the tag as printed on the plans to label a wall: "1", "A3",
  "2C", "P1". Copy it exactly, without the circle, diamond or hexagon around
  it.
- `description` is what the wall is built of, as written: stud size, board,
  layers, rating.

Include a type only if this sheet actually defines it. Do not invent symbols,
do not include door types, keynotes, detail callouts, revision marks, room
numbers or accessory schedules -- those are different things that look alike.

If the sheet defines no wall types at all, return {"types": []}."""


async def read_legend(png: bytes) -> tuple[list[dict[str, str]], list[str]]:
    """The wall types drawn on one sheet. Returns (types, warnings).

    Failure is never fatal. A set whose legend cannot be read loses its wall
    types and keeps its doors, exactly as it did before this existed.
    """
    try:
        client = get_client()
    except AiUnavailableError:
        return [], ["wall legend not read: no AI key configured"]

    settings = get_settings()
    data_url = "data:image/png;base64," + base64.b64encode(png).decode()
    try:
        response = await client.chat.completions.create(
            model=settings.ai_model,
            temperature=0,
            max_tokens=1200,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": [
                    {"type": "text", "text": _PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ]},
            ],
        )
        payload = json.loads(response.choices[0].message.content or "{}")
    except Exception as exc:  # noqa: BLE001 - a tier failure is a warning
        return [], [f"wall legend could not be read ({exc})"]

    raw = payload.get("types")
    if not isinstance(raw, list):
        return [], ["wall legend returned an unexpected shape"]

    out: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        symbol = str(entry.get("symbol", "")).strip()
        # A symbol is short. Anything longer is the model describing rather
        # than quoting, and it would poison the plan search.
        if not symbol or len(symbol) > 4:
            continue
        out.append({"symbol": symbol,
                    "description": str(entry.get("description", ""))[:200]})

    log.info("wall legend read by AI: %d type(s): %s",
             len(out), ", ".join(t["symbol"] for t in out) or "none")
    return out, []
