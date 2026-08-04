"""Resolve column headers the static alias table does not recognise.

Every architecture firm names columns differently -- `HW` / `HDW` / `HDWE SET` /
`HARDWARE GROUP` all mean the same thing, and no fixed list survives contact
with the next office's title block. This sends *only the header strings*, never
the table, so it costs tens of tokens rather than an image.

Results are cached per header signature: a firm's sheets repeat, so the second
document from the same office costs nothing.
"""

from __future__ import annotations

import json
import logging

from app.ai.client import AiUnavailableError, get_client
from app.config import get_settings
from app.schemas import CANONICAL_FIELDS

log = logging.getLogger(__name__)

# Header signature -> {raw header: canonical field}. Process-lifetime cache;
# this service is stateless by design, so nothing is persisted.
_CACHE: dict[tuple[str, ...], dict[str, str]] = {}

# Example cells shown per column. Enough to tell an identifier from a code,
# few enough that this stays a header question rather than a table dump.
_SAMPLE_VALUES = 3

_SYSTEM = (
    "You map column headers from a construction door schedule onto a fixed set "
    "of field names. You never invent data and never guess when a header has no "
    "reasonable equivalent."
)

_FIELD_NOTES = {
    "door_tag": "the door/opening number or mark",
    "from_space": "room the door swings from",
    "to_space": "room the door leads to",
    "door_width": "width of the door leaf or opening",
    "door_height": "height of the door leaf or opening",
    "door_type": "door type letter or code",
    "door_material": "what the door leaf is made of",
    "door_finish": "finish applied to the door leaf",
    "frame_material": "what the frame is made of",
    "frame_finish": "finish applied to the frame",
    "threshold": "threshold or sill reference",
    "fire_rating": "fire rating or label",
    "hw_set": "hardware set / group number",
    "comments": "remarks or notes",
}


def _prompt(unknown: list[str], samples: dict[str, list[str]] | None) -> str:
    fields = "\n".join(f"  {name}: {note}" for name, note in _FIELD_NOTES.items())

    if samples:
        # A heading alone cannot say whether a column is the row's identifier or
        # a type code: "TYPE" holding 1, 2, 3 is the door number, and a column
        # headed SIGN was being mapped to hw_set on its name alone.
        listing = "\n".join(
            f"  {header}: {', '.join(samples[header][:_SAMPLE_VALUES])}"
            for header in unknown if samples.get(header)
        )
        columns = f"Headers, with example values from each column:\n{listing}\n"
    else:
        columns = f"Headers: {json.dumps(unknown)}\n"

    return (
        "Map each column header to one of these fields, or to null when no field "
        "genuinely fits.\n\n"
        f"Fields:\n{fields}\n\n"
        f"{columns}\n"
        "Rules:\n"
        "- Ignore group prefixes that only say which part of the assembly a "
        "column belongs to, such as 'DOOR (AS APPLICABLE)'. Judge the column by "
        "its own heading.\n"
        "- A 'TYPE' column holds a type code, not a material or a finish. "
        "'FRAME TYPE' is NOT frame_material; return null for it.\n"
        "- HDW, HW, HDWE, HARDWARE and HARDWARE SET all mean hw_set.\n"
        "- Thickness, gauge, detail references (head/jamb/sill), glazing, "
        "louvers, signage and lock function have no field here. Return null.\n"
        "- Judge by the example values as much as the heading. A column of "
        "short values that identify each row one by one -- 101, 102, 103A -- is "
        "door_tag, whatever it is headed.\n"
        "- Do not map two headers to the same field.\n"
        "- Prefer null over a loose fit. A wrong mapping silently corrupts a "
        "row.\n\n"
        'Return JSON: {"mapping": {"<header>": "<field or null>"}}'
    )


async def resolve_headers(unknown: list[str],
                          samples: dict[str, list[str]] | None = None
                          ) -> tuple[dict[str, str], list[str]]:
    """Map unrecognised headers to canonical fields. Returns (mapping, warnings).

    `samples` gives a few example values per column. A heading alone cannot say
    whether a column identifies the row or classifies it, which is how door
    numbers ended up in door_type and a SIGN column became hw_set.

    Still no full table: a handful of cells, never the rows themselves.

    Failure is never fatal: an unresolved header simply stays in `extra`, which
    is where it already was.
    """
    warnings: list[str] = []
    cleaned = [h for h in dict.fromkeys(unknown) if h and h.strip()]
    if not cleaned:
        return {}, warnings

    signature = tuple(sorted(cleaned))
    if signature in _CACHE:
        return dict(_CACHE[signature]), warnings

    try:
        client = get_client()
    except AiUnavailableError:
        return {}, ["header mapping skipped: no AI key configured"]

    settings = get_settings()
    try:
        response = await client.chat.completions.create(
            model=settings.ai_model,
            temperature=0,
            max_tokens=600,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _prompt(cleaned, samples)},
            ],
        )
        payload = json.loads(response.choices[0].message.content or "{}")
    except Exception as exc:  # noqa: BLE001 - a tier failure is a warning
        return {}, [f"header mapping failed, columns kept as extras ({exc})"]

    raw = payload.get("mapping")
    if not isinstance(raw, dict):
        return {}, ["header mapping returned an unexpected shape"]

    mapping: dict[str, str] = {}
    taken: set[str] = set()
    for header, field in raw.items():
        if not isinstance(field, str) or field not in CANONICAL_FIELDS:
            continue
        if field in taken:  # the model was told not to; enforce it anyway
            continue
        mapping[str(header)] = field
        taken.add(field)

    usage = getattr(response, "usage", None)
    if usage:
        log.info("header_map tokens prompt=%s completion=%s",
                 usage.prompt_tokens, usage.completion_tokens)
    if mapping:
        warnings.append(
            "column headers resolved by AI: "
            + ", ".join(f"{k} -> {v}" for k, v in mapping.items())
        )

    _CACHE[signature] = dict(mapping)
    return mapping, warnings
