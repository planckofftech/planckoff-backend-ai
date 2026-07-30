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


def _salvage_arrays(text: str) -> list[list[str]]:
    """Recover every row array that closed before the response was cut off.

    Rows sit nested inside the `rows` container, so every nesting level has to
    be tracked -- scanning only the outermost array finds the header row and
    nothing else, because the container itself never closes when truncated.
    Arrays of arrays are skipped; only all-scalar arrays are rows.
    """
    out: list[list[str]] = []
    starts: list[int] = []
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
        elif ch == "[":
            starts.append(idx)
        elif ch == "]" and starts:
            start = starts.pop()
            try:
                row = json.loads(text[start:idx + 1])
            except json.JSONDecodeError:
                continue
            if isinstance(row, list) and all(
                isinstance(c, (str, int, float, type(None))) for c in row
            ):
                out.append(["" if c is None else str(c) for c in row])
    return out


def parse_table(content: str) -> tuple[list[str], list[list[str]], list[str]]:
    """Parse {"headers": [...], "rows": [[...]]} from a model response.

    Returns (headers, rows, warnings). Survives markdown fences, prose around
    the JSON, and truncation at the token limit.
    """
    warnings: list[str] = []
    if not content or not content.strip():
        return [], [], ["model returned an empty response"]

    text = _strip_fences(content)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        salvaged = _salvage_arrays(text)
        if len(salvaged) < 2:
            return [], [], ["model response could not be parsed as JSON"]
        # The first complete array is the header row; the rest are data.
        headers, rows = salvaged[0], salvaged[1:]
        warnings.append(
            f"model response was truncated; salvaged {len(rows)} complete rows"
        )
        return headers, rows, warnings

    if not isinstance(parsed, dict):
        return [], [], ["model response was not a JSON object"]

    headers = [str(h) for h in parsed.get("headers") or [] if h is not None]
    raw_rows = parsed.get("rows") or []
    rows: list[list[str]] = []
    ragged = 0
    for row in raw_rows:
        if isinstance(row, dict) and headers:
            # The requested shape. A missing key is a blank cell and cannot
            # shift the cells after it.
            rows.append([_cell(row, h) for h in headers])
        elif isinstance(row, list):
            # Positional fallback. Models drop blank cells rather than padding
            # them, which silently shifts every value after the gap -- so a
            # length mismatch has to be reported, not quietly accepted.
            if headers and len(row) != len(headers):
                ragged += 1
            padded = ["" if c is None else str(c) for c in row]
            padded += [""] * (len(headers) - len(padded))
            rows.append(padded[:len(headers)] if headers else padded)

    if not headers:
        warnings.append("model returned no column headers")
    if ragged:
        warnings.append(
            f"{ragged} of {len(rows)} rows did not have one cell per column; "
            "values in those rows may be shifted"
        )
    return headers, rows, warnings


def _cell(row: dict, header: str) -> str:
    """Look up a cell, tolerating case and whitespace drift in the key."""
    if header in row:
        return "" if row[header] is None else str(row[header]).strip()
    wanted = header.strip().casefold()
    for key, value in row.items():
        if str(key).strip().casefold() == wanted:
            return "" if value is None else str(value).strip()
    return ""


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
