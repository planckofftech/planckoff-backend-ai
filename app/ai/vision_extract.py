"""Phase 4 -- vision fallback for pages whose structure is genuinely gone.

Only ever handed candidate pages, never the document. A vision model given an
unreadable page invents plausible rows rather than reporting failure, so every
gate that keeps us out of here is protecting accuracy, not just tokens.
"""

from __future__ import annotations

import base64
import logging

from app.ai.client import AiUnavailableError, AiUpstreamError, get_client
from app.ai.response_parser import parse_rows
from app.config import get_settings
from app.core.pdf_doc import PdfDoc
from app.schemas import CANONICAL_FIELDS, DoorRow

log = logging.getLogger(__name__)

_PROMPT = (
    "This image is one sheet from a construction document. Extract the DOOR "
    "SCHEDULE table only.\n\n"
    "Rules:\n"
    "- Ignore the hardware schedule, title block, notes, and any other table.\n"
    "- One object per door row, in sheet order.\n"
    "- Copy values exactly as printed. Do not normalize, expand or invent.\n"
    "- Use an empty string for a cell that is blank on the sheet.\n"
    "- A row with no door number is still a row if it has other values.\n"
    "- If you cannot read the table, return an empty rows array. Never guess.\n\n"
    'Return JSON: {"rows": [{' +
    ", ".join(f'"{f}": ""' for f in CANONICAL_FIELDS) +
    "}]}"
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
    raw_rows, parse_warnings = parse_rows(content)
    warnings.extend(parse_warnings)

    usage = getattr(response, "usage", None)
    if usage:
        log.info("ai_vision tokens prompt=%s completion=%s",
                 usage.prompt_tokens, usage.completion_tokens)

    rows: list[DoorRow] = []
    for raw in raw_rows:
        values = {
            f: str(raw.get(f) or "").strip() for f in CANONICAL_FIELDS if raw.get(f)
        }
        extra = {
            str(k): str(v).strip() for k, v in raw.items()
            if k not in CANONICAL_FIELDS and k != "extra" and v
        }
        if values:
            rows.append(DoorRow(**values, extra=extra))

    return rows, warnings
