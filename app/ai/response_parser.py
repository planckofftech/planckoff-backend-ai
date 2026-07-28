"""Salvage structured rows from an imperfect model response.

Must survive, in order of how often they actually happen: markdown code fences,
a bare array instead of the expected envelope, prose before the JSON, and
truncation at the token limit.
"""

from __future__ import annotations

import json
import re

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def _strip_fences(text: str) -> str:
    match = _FENCE.search(text)
    return match.group(1) if match else text.strip()


def _salvage_objects(text: str) -> list[dict]:
    """Walk a truncated array and recover every object that did close.

    A response cut off at the token limit is still worth most of its rows; the
    alternative is discarding a paid call over one missing bracket.
    """
    out: list[dict] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for idx, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    obj = json.loads(text[start:idx + 1])
                except json.JSONDecodeError:
                    pass
                else:
                    if isinstance(obj, dict):
                        out.append(obj)
                start = -1
            elif depth < 0:
                depth = 0
    return out


def parse_rows(content: str) -> tuple[list[dict], list[str]]:
    """Returns (row dicts, warnings)."""
    warnings: list[str] = []
    if not content or not content.strip():
        return [], ["model returned an empty response"]

    text = _strip_fences(content)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        rows = _salvage_objects(text)
        if rows:
            warnings.append(
                f"model response was not valid JSON; salvaged {len(rows)} complete rows"
            )
            # The envelope object, if it survived, is not a row.
            return [r for r in rows if any(k in r for k in
                                           ("door_tag", "from_space", "to_space"))], warnings
        return [], ["model response could not be parsed as JSON"]

    if isinstance(parsed, list):
        rows = [r for r in parsed if isinstance(r, dict)]
        return rows, warnings
    if isinstance(parsed, dict):
        for key in ("rows", "doors", "door_schedule", "data", "items"):
            value = parsed.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)], warnings
        # A single row returned bare.
        if any(k in parsed for k in ("door_tag", "from_space", "to_space")):
            return [parsed], warnings
    return [], ["model response had no recognizable rows array"]
