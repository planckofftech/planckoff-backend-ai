"""Phase 4 -- vision fallback for pages whose structure is genuinely gone.

Only ever handed candidate pages, never the document. A vision model given an
unreadable page invents plausible rows rather than reporting failure, so every
gate that keeps us out of here is protecting accuracy, not just tokens.
"""

from __future__ import annotations

import base64
import logging

from app.ai.client import AiUnavailableError, AiUpstreamError, get_client
from app.ai.header_map import resolve_headers
from app.ai.response_parser import parse_table
from app.config import get_settings
from app.core import header_mapper
from app.core.pdf_doc import PdfDoc
from app.schemas import DoorRow

log = logging.getLogger(__name__)

# Below this, the static alias table is doing fine and a call is not worth it.
_AI_HEADER_THRESHOLD = 2

# Ask for the sheet's OWN columns, not our field names. Forcing a fixed schema
# made the model displace real values: on a sheet with THK / LOCK FUNCTION /
# FRAME TYPE columns it put the thickness "1 3/4"" into door_material and shifted
# every column after it. Reporting the table as printed keeps our mapping in one
# place -- the header mapper -- shared with the deterministic path.
_PROMPT = (
    "This image is one sheet from a construction document. Find the schedule "
    "that lists the doors or openings and transcribe it.\n\n"
    "The table may be titled DOOR SCHEDULE, FRAME OPENING SCHEDULE, DOOR AND "
    "HARDWARE SCHEDULE or similar. Hardware, frame and detail columns are part "
    "of that table -- include them. Ignore door-type elevations, general notes, "
    "material keys and the title block.\n\n"
    "Rules:\n"
    "- Report the column headers exactly as printed, left to right.\n"
    "- Where headers are stacked (a group above a sub-heading), join them with "
    "a space, e.g. 'FRAME MATERIAL'.\n"
    "- One array per row, in sheet order, with one cell per header. Pad short "
    "rows with empty strings so every row has the same length as headers.\n"
    "- Copy values exactly as printed. Do not normalise, expand, or invent.\n"
    "- Use an empty string for a cell that is blank on the sheet.\n"
    "- A row with no door number is still a row if it has other values.\n"
    "- If you cannot read a table, return empty arrays. Never guess.\n\n"
    'Return JSON: {"headers": ["..."], "rows": [["..."]]}'
)


def _upstream_reason(exc: Exception) -> str:
    """Turn a provider error into something the caller can act on.

    "Error code: 401" tells an operator nothing; "the key was rejected, check
    OPENROUTER_API_KEY" tells them exactly what to go and do.
    """
    status = getattr(exc, "status_code", None)
    hints = {
        401: "OpenRouter rejected the API key -- check OPENROUTER_API_KEY "
             "(a deleted or revoked key reports 'User not found')",
        402: "OpenRouter reports insufficient credit for this request",
        403: "OpenRouter refused this model for this key",
        404: "OpenRouter does not recognise the configured AI_MODEL",
        429: "OpenRouter rate-limited this key; retry shortly",
    }
    hint = hints.get(status)
    return f"{hint} [{exc}]" if hint else str(exc)


async def extract_with_vision(doc: PdfDoc, page: int) -> tuple[list[DoorRow], list[str]]:
    """Render one page and ask the model. Returns (rows, warnings)."""
    settings = get_settings()
    warnings: list[str] = []

    try:
        client = get_client()
    except AiUnavailableError as exc:
        return [], [f"AI fallback skipped: {exc}"]

    png = doc.render_png(page - 1, dpi=settings.ai_render_dpi)
    data_url = "data:image/png;base64," + base64.b64encode(png).decode()
    log.info("ai_vision page=%s dpi=%s png_kb=%.0f",
             page, settings.ai_render_dpi, len(png) / 1024)

    try:
        response = await client.chat.completions.create(
            model=settings.ai_model,
            temperature=0,
            max_tokens=8000,
            response_format={"type": "json_object"},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": _PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }],
        )
    except Exception as exc:  # noqa: BLE001 - surface the upstream reason as-is
        raise AiUpstreamError(_upstream_reason(exc)) from exc

    content = response.choices[0].message.content or ""
    headers, raw_rows, parse_warnings = parse_table(content)
    warnings.extend(parse_warnings)

    usage = getattr(response, "usage", None)
    if usage:
        log.info("ai_vision tokens prompt=%s completion=%s",
                 usage.prompt_tokens, usage.completion_tokens)

    if not headers or not raw_rows:
        return [], warnings

    rows, map_warnings = await rows_from_table(headers, raw_rows)
    warnings.extend(map_warnings)
    return rows, warnings


async def rows_from_table(headers: list[str], raw_rows: list[list[str]]
                          ) -> tuple[list[DoorRow], list[str]]:
    """Map a transcribed table onto DoorRow through the shared header mapper.

    The same alias table and the same `extra` escape hatch as the deterministic
    path, so a column with no canonical equivalent is preserved rather than
    displacing a real field.
    """
    warnings: list[str] = []
    mapped, unmapped = header_mapper.map_headers(headers)

    if len(unmapped) >= _AI_HEADER_THRESHOLD:
        overrides, hint_warnings = await resolve_headers(unmapped)
        warnings.extend(hint_warnings)
        if overrides:
            mapped, unmapped = header_mapper.map_headers(headers, overrides)

    if unmapped:
        warnings.append(
            f"columns kept under 'extra': {', '.join(unmapped)}"
        )

    rows: list[DoorRow] = []
    for cells in raw_rows:
        values: dict[str, str] = {}
        extra: dict[str, str] = {}
        for idx, cell in enumerate(cells):
            if idx >= len(headers):
                break
            text = str(cell or "").strip()
            if not text:
                continue
            field = mapped[idx] if idx < len(mapped) else None
            if field:
                values[field] = text
            else:
                extra[header_mapper.extra_key(headers[idx], idx)] = text
        if values or extra:
            rows.append(DoorRow(**values, extra=extra))

    return rows, warnings
