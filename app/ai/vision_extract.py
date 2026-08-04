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
from app.ai.response_parser import parse_box, parse_table
from app.config import get_settings
from app.core import header_mapper
from app.core.pdf_doc import PdfDoc
from app.schemas import DoorRow, TableBox

log = logging.getLogger(__name__)

# Below this, the static alias table is doing fine and a call is not worth it.
_AI_HEADER_THRESHOLD = 2
# Extra attempts when the provider answers 200 with an empty completion.
_EMPTY_RETRIES = 2
_MAX_OUTPUT_TOKENS = 32000
# Matches needed before the text layer is trusted to place the table, and the
# shortest value worth matching -- "A", "1" and "-" occur all over a sheet.
_MIN_TEXT_MATCHES = 8
_MIN_MATCH_LENGTH = 3
# Slack added around a located table, as a fraction of its own size.
_BOX_PAD = 0.02

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
    "- Each row is an OBJECT whose keys are those exact header strings.\n"
    "- Omit a key, or give it \"\", when that cell is blank on the sheet.\n"
    "- Copy values exactly as printed. Do not normalise, expand, or invent.\n"
    "- A row with no door number is still a row if it has other values.\n"
    "- If you cannot read a table, return empty arrays. Never guess.\n\n"
    "Also report where that table sits on the image, as whole numbers from 0 "
    "to 1000 measured from the top-left corner: x across, y down. Cover the "
    "caption and every column, nothing else. Omit `box` if unsure.\n\n"
    'Return JSON: {"headers": ["..."], "rows": [{"<header>": "<value>"}], '
    '"box": {"x0": 0, "y0": 0, "x1": 1000, "y1": 1000}}'
)


def box_from_text(doc: PdfDoc, page: int, rows: list[DoorRow]
                  ) -> tuple[float, float, float, float] | None:
    """Where the values the model read actually sit, as fractions of the page.

    Better than any rectangle the model draws, whenever the page has a text
    layer at all: these are the very strings it transcribed, so the area they
    occupy *is* the table. A true scan has no text to match and falls back to
    the model's own estimate.

    Values only, never headers: a heading like TYPE or FINISH reappears in
    every other schedule on the sheet and would stretch the box across all of
    them.
    """
    items = [i for i in doc.text_items(page - 1) if i.horizontal]
    if not items:
        return None

    wanted = {
        value.strip() for row in rows
        for value in list(row.model_dump(exclude={"extra"}).values())
        + list(row.extra.values())
        if isinstance(value, str) and len(value.strip()) >= _MIN_MATCH_LENGTH
    }
    if not wanted:
        return None
    hits = [i for i in items if i.text.strip() in wanted]
    if len(hits) < _MIN_TEXT_MATCHES:
        return None

    width, height = doc.page_size(page - 1)
    if width <= 0 or height <= 0:
        return None
    return (min(i.x0 for i in hits) / width, min(i.y0 for i in hits) / height,
            max(i.x1 for i in hits) / width, max(i.y1 for i in hits) / height)


def _padded(page: int, box: tuple[float, float, float, float] | None,
            source: str) -> TableBox | None:
    """Widen the rectangle slightly before handing it to the preview.

    Both sources mark where the *values* are. A box drawn tight to them clips
    the caption and the table's outer rule -- better a little generous than
    visibly cutting off the thing it is pointing at.
    """
    if box is None:
        return None
    x0, y0, x1, y1 = box
    pad_x, pad_y = (x1 - x0) * _BOX_PAD, (y1 - y0) * _BOX_PAD
    return TableBox(
        page=page, source=source,
        x0=max(0.0, x0 - pad_x), y0=max(0.0, y0 - pad_y),
        x1=min(1.0, x1 + pad_x), y1=min(1.0, y1 + pad_y),
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
        500: "the model provider failed on OpenRouter's side; this is usually "
             "transient -- retry",
        502: "OpenRouter could not reach the model provider; usually transient "
             "-- retry",
        503: "the model provider is unavailable on OpenRouter; usually "
             "transient -- retry",
    }
    hint = hints.get(status)
    if hint is None and status is None:
        hint = ("could not reach OpenRouter at all -- check network access to "
                "openrouter.ai")
    return f"{hint} [{exc}]" if hint else str(exc)


async def extract_with_vision(
    doc: PdfDoc, page: int
) -> tuple[list[DoorRow], list[str], list[str], TableBox | None]:
    """Render one page and ask the model.

    Returns (rows, headers, warnings, box). `box` is where the table sits on
    the page, or None when neither the text layer nor the model could place it.
    """
    settings = get_settings()
    warnings: list[str] = []

    try:
        client = get_client()
    except AiUnavailableError as exc:
        return [], [], [f"AI fallback skipped: {exc}"], None

    png = doc.render_png(page - 1, dpi=settings.ai_render_dpi)
    data_url = "data:image/png;base64," + base64.b64encode(png).decode()
    log.info("ai_vision page=%s dpi=%s png_kb=%.0f",
             page, settings.ai_render_dpi, len(png) / 1024)

    # The provider intermittently answers 200 with an empty completion -- zero
    # tokens, no content. That is not an HTTP error, so the SDK's own retries
    # never see it, and one bad draw was turning a readable sheet into "no door
    # schedule found". Ask again before giving up.
    headers: list[str] = []
    raw_rows: list[list[str]] = []
    parse_warnings: list[str] = []
    model_box: tuple[float, float, float, float] | None = None
    for attempt in range(1, _EMPTY_RETRIES + 2):
        try:
            response = await client.chat.completions.create(
                model=settings.ai_model,
                temperature=0,
                # A 50-row schedule keyed by header is a lot of output, and a
                # reply cut off at the limit parsed as nothing at all. Gemini
                # allows far more than the 8000 this used to ask for.
                max_tokens=_MAX_OUTPUT_TOKENS,
                response_format={"type": "json_object"},
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }],
            )
        except Exception as exc:  # noqa: BLE001 - surface the upstream reason
            raise AiUpstreamError(_upstream_reason(exc)) from exc

        usage = getattr(response, "usage", None)
        if usage:
            log.info("ai_vision tokens prompt=%s completion=%s attempt=%s",
                     usage.prompt_tokens, usage.completion_tokens, attempt)

        content = response.choices[0].message.content or ""
        headers, raw_rows, parse_warnings = parse_table(content)
        model_box = parse_box(content)
        if headers and raw_rows:
            break
        if attempt <= _EMPTY_RETRIES:
            log.warning("ai_vision returned nothing usable, retrying (%s)", attempt)

    warnings.extend(parse_warnings)

    if not headers or not raw_rows:
        return [], headers, warnings, None

    rows, map_warnings = await rows_from_table(headers, raw_rows)
    warnings.extend(map_warnings)

    box = (_padded(page, box_from_text(doc, page, rows), "text")
           or _padded(page, model_box, "model"))
    log.info("ai_vision box page=%s %s", page, box)
    return rows, headers, warnings, box


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
        # Example cells from the transcribed table, so the mapper can tell a
        # row identifier from a type code.
        samples: dict[str, list[str]] = {}
        for index, header in enumerate(headers):
            if header not in unmapped:
                continue
            seen = [str(r[index]).strip() for r in raw_rows
                    if index < len(r) and str(r[index]).strip()]
            if seen:
                samples[header] = list(dict.fromkeys(seen))[:3]
        overrides, hint_warnings = await resolve_headers(unmapped, samples)
        warnings.extend(hint_warnings)
        if overrides:
            mapped, unmapped = header_mapper.map_headers(headers, overrides)

    columns = [[str(r[i]).strip() if i < len(r) else "" for r in raw_rows]
               for i in range(len(headers))]
    before = list(mapped)
    mapped = header_mapper.infer_tag_column(mapped, columns, headers)
    if mapped != before:
        claimed = headers[mapped.index("door_tag")]
        warnings.append(
            f"no column named the door tag; read it from the data in {claimed!r}"
        )
        unmapped = [h for h in unmapped if h != claimed]

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

