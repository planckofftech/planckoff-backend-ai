"""Phase 4 -- vision fallback for pages whose structure is genuinely gone.

Only ever handed candidate pages, never the document. A vision model given an
unreadable page invents plausible rows rather than reporting failure, so every
gate that keeps us out of here is protecting accuracy, not just tokens.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field

from app.ai.client import AiUnavailableError, AiUpstreamError, get_client
from app.ai.header_map import resolve_headers
from app.ai.response_parser import parse_tables
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
# The provider refuses an image over 30 MB; stay clear of it, and step the
# resolution down until a page fits.
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_FALLBACK_DPI = (150, 120, 96)
# Matches needed before the text layer is trusted to place the table, and the
# shortest value worth matching -- "A", "1" and "-" occur all over a sheet.
# Door numbers found standing in one column before a table is placed by them,
# and how far apart two of them may stand and still be one column (points).
_MIN_TAG_HITS = 3
_COLUMN_TOL = 8.0
# Rows of heading and caption above a table's first door.
_HEADING_ROWS = 3
# Share of a table's door numbers that must be printed on a page that prints
# its door numbers, for the table to count as read from that page.
_PRINTED_SHARE = 0.5
# How far past the model's own box for a table its edge may be drawn, as a
# fraction of the page. A safety margin, not an edge: the model's boxes move
# between readings of the same sheet, and drawn tight to one they cut a table
# short. The real edges are the tables beside it.
_MODEL_SLACK = 0.10
# Slack added around a located table, as a fraction of its own size.
_BOX_PAD = 0.02

# Ask for the sheet's OWN columns, not our field names. Forcing a fixed schema
# made the model displace real values: on a sheet with THK / LOCK FUNCTION /
# FRAME TYPE columns it put the thickness "1 3/4"" into door_material and shifted
# every column after it. Reporting the table as printed keeps our mapping in one
# place -- the header mapper -- shared with the deterministic path.
_PROMPT = (
    "This image is one sheet from a construction document. Find every "
    "schedule on it that lists doors or openings, and transcribe each one.\n\n"
    "A table may be titled DOOR SCHEDULE, FRAME OPENING SCHEDULE, DOOR AND "
    "HARDWARE SCHEDULE or similar. A sheet often carries several -- one per "
    "level, or a schedule continued in a second table beside the first. "
    "Report each printed table as its own entry, in reading order; never merge "
    "two tables into one. Hardware, frame and detail columns are part of a "
    "table -- include them. Ignore door-type elevations, general notes, "
    "material keys and the title block.\n\n"
    "Rules:\n"
    "- Report the column headers exactly as printed, left to right.\n"
    "- Where headers are stacked (a group above a sub-heading), join them with "
    "a space, e.g. 'FRAME MATERIAL'.\n"
    "- Each row is an OBJECT whose keys are those exact header strings.\n"
    "- Omit a key, or give it \"\", when that cell is blank on the sheet.\n"
    "- Copy values exactly as printed. Do not normalise, expand, or invent.\n"
    "- A row with no door number is still a row if it has other values.\n"
    "- If you cannot read a table, leave it out. Never guess.\n\n"
    "For each table, also report where it sits on the image, as whole numbers "
    "from 0 to 1000 measured from the top-left corner: x across, y down. Cover "
    "that table's caption and every column, nothing else. Omit `box` if "
    "unsure.\n\n"
    'Return JSON: {"tables": [{"title": "<caption as printed>", '
    '"headers": ["..."], "rows": [{"<header>": "<value>"}], '
    '"box": {"x0": 0, "y0": 0, "x1": 1000, "y1": 1000}}]}'
)


def boxes_from_text(doc: PdfDoc, page: int, tables: list[list[DoorRow]],
                    model_boxes: list | None = None
                    ) -> list[tuple[float, float, float, float] | None]:
    """Where each table sits, as fractions of the page, found by its own doors.

    Better than any rectangle the model draws whenever the page has a text
    layer: these are the very strings it transcribed. But not just any of them
    -- "3'-0\" X 7'-0\"", "1 3/4\"" and "-" are in every schedule on a sheet,
    and matching them put three schedules in one identical box. A table's door
    numbers are its own. They give its door-number column, its first and last
    rows, and so its left edge, top and bottom; the right edge is where the
    next table starts on the same rows -- its door-number column, or, for a
    table with none such as a hardware schedule, the model's box for it -- or
    else the last of this table's values on them, never far past the model's
    own box for this one. Without that, the dashes and letters in the notes
    beside a schedule carried its box across the title block.

    None for a table whose doors cannot be found as text -- a true scan -- so
    the caller falls back to the model's own estimate.
    """
    items = [i for i in doc.text_items(page - 1) if i.horizontal]
    width, height = doc.page_size(page - 1)
    if not items or width <= 0 or height <= 0:
        return [None] * len(tables)

    columns = [_tag_column(items, rows) for rows in tables]
    model_boxes = model_boxes or [None] * len(tables)
    # Where every table starts, in points: its door numbers if it has any,
    # otherwise the model's box -- (left, top, bottom).
    starts = []
    for column, box in zip(columns, model_boxes):
        if column is not None:
            starts.append((column[0], column[1], column[2]))
        elif box is not None:
            starts.append((box[0] * width, box[1] * height, box[3] * height))
        else:
            starts.append(None)
    out: list[tuple[float, float, float, float] | None] = []
    for k, rows in enumerate(tables):
        column = columns[k]
        if column is None:
            out.append(None)
            continue
        left, top, bottom, row_height = column
        # The next table to the right on the same rows ends this one.
        limit = min((c[0] for j, c in enumerate(starts)
                     if j != k and c is not None and c[0] > left + _COLUMN_TOL
                     and c[1] < bottom and c[2] > top), default=width)
        own = model_boxes[k]
        if own is not None:
            limit = min(limit, (own[2] + _MODEL_SLACK) * width)
        values = {v.strip() for r in rows
                  for v in list(r.model_dump(exclude={"extra"}).values())
                  + list(r.extra.values())
                  if isinstance(v, str) and v.strip()}
        band = [i for i in items
                if top - 1 <= i.cy <= bottom + 1 and left - 2 <= i.x0 < limit
                and i.x1 <= limit and i.text.strip() in values]
        right = max((i.x1 for i in band), default=left + width * 0.1)
        # Room above the first row for the column headings and caption.
        top -= row_height * _HEADING_ROWS
        out.append((max(0.0, left) / width, max(0.0, top) / height,
                    min(width, right) / width, min(height, bottom) / height))
    return out


def _tag_column(items, rows: list[DoorRow]
                ) -> tuple[float, float, float, float] | None:
    """(left, top, bottom, row height) of this table's door-number column."""
    tags = {r.door_tag.strip() for r in rows if r.door_tag and r.door_tag.strip()}
    hits = [i for i in items if i.text.strip() in tags]
    if not hits:
        return None
    # The column is where most of them line up; a door number repeated in a
    # note elsewhere on the sheet stands alone.
    best: list = []
    for anchor in hits:
        group = [i for i in hits if abs(i.x0 - anchor.x0) <= _COLUMN_TOL]
        if len(group) > len(best):
            best = group
    if len(best) < min(_MIN_TAG_HITS, len(tags)):
        return None
    ys = sorted(i.cy for i in best)
    gaps = sorted(b - a for a, b in zip(ys, ys[1:]) if b > a)
    row_height = gaps[len(gaps) // 2] if gaps else max(i.y1 - i.y0 for i in best)
    return (min(i.x0 for i in best), min(i.y0 for i in best),
            max(i.y1 for i in best), row_height)


def _render_within_limit(doc: PdfDoc, page: int, dpi: int) -> bytes:
    """The page as a PNG the provider will accept.

    A large sheet at full resolution is too big to send: one 69-sheet site set
    rendered past the 30 MB the provider takes, and the whole read failed on
    an HTTP 413. Each step down keeps the drawing legible; the smallest is
    still well above what a schedule's text needs.
    """
    png = b""
    for attempt in (dpi, *(d for d in _FALLBACK_DPI if d < dpi)):
        png = doc.render_png(page - 1, dpi=attempt)
        if len(png) <= _MAX_IMAGE_BYTES:
            if attempt != dpi:
                log.info("ai_vision page=%s rendered at %s dpi to fit", page, attempt)
            return png
    return png


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


@dataclass(slots=True)
class VisionTable:
    """One door schedule read off a sheet image."""
    title: str
    headers: list[str]
    rows: list[DoorRow]
    box: TableBox | None
    warnings: list[str] = field(default_factory=list)


async def read_tables(doc: PdfDoc, page: int) -> list[VisionTable]:
    """Render one page and ask the model for every door schedule on it.

    Each table comes back with its own rows and its own box, so a sheet with
    three schedules is three tables, each outlined where it sits -- not one
    table in one box around all three.
    """
    settings = get_settings()
    try:
        client = get_client()
    except AiUnavailableError as exc:
        log.info("vision read skipped: %s", exc)
        return []

    png = _render_within_limit(doc, page, settings.ai_render_dpi)
    data_url = "data:image/png;base64," + base64.b64encode(png).decode()
    log.info("ai_vision page=%s dpi=%s png_kb=%.0f",
             page, settings.ai_render_dpi, len(png) / 1024)

    # The provider intermittently answers 200 with an empty completion -- zero
    # tokens, no content. That is not an HTTP error, so the SDK's own retries
    # never see it, and one bad draw was turning a readable sheet into "no door
    # schedule found". Ask again before giving up.
    parsed = []
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

        parsed = parse_tables(response.choices[0].message.content or "")
        if parsed:
            break
        if attempt <= _EMPTY_RETRIES:
            log.warning("ai_vision returned nothing usable, retrying (%s)", attempt)

    # On a page whose door numbers are printed as text, what the model read
    # can be checked against what is printed. Asked for every schedule on one
    # sheet, it returned a fifth table of doors 234 to 260 on a sheet that ends
    # at 233B -- rows it made up. A table whose door numbers are mostly not on
    # such a page was not read from it.
    #
    # Only where the page has shown its door numbers are text: another sheet
    # carries plenty of text but draws every door number as linework, and
    # there the check would throw out the real schedule. A scan, likewise, is
    # taken on trust.
    printed = {i.text.strip() for i in doc.text_items(page - 1)}
    read: list[tuple] = []
    for title, headers, raw_rows, model_box, parse_warnings in parsed:
        rows, map_warnings = await rows_from_table(headers, raw_rows)
        if not rows:
            continue
        tags = [r.door_tag.strip() for r in rows if r.door_tag and r.door_tag.strip()]
        found = sum(t in printed for t in tags) / len(tags) if tags else None
        read.append((title, headers, rows, model_box, parse_warnings + map_warnings, found))
    checkable = any(f is not None and f >= _PRINTED_SHARE for *_, f in read)

    tables: list[VisionTable] = []
    model_boxes = []
    for title, headers, rows, model_box, warnings, found in read:
        if checkable and found is not None and found < _PRINTED_SHARE:
            log.info("ai_vision page=%s dropped a table not found on the page", page)
            continue
        tables.append(VisionTable(title, headers, rows, None, warnings))
        model_boxes.append(model_box)
    # Placed together, because each table's right edge is the next one's
    # door-number column.
    placed = boxes_from_text(doc, page, [t.rows for t in tables], model_boxes)
    for table, text_box, model_box in zip(tables, placed, model_boxes):
        table.box = (_padded(page, text_box, "text")
                     or _padded(page, model_box, "model"))
    log.info("ai_vision page=%s tables=%s rows=%s", page, len(tables),
             [len(t.rows) for t in tables])
    return tables


async def extract_with_vision(
    doc: PdfDoc, page: int
) -> tuple[list[DoorRow], list[str], list[str], TableBox | None]:
    """The page's door schedules as one list of rows.

    Returns (rows, headers, warnings, box) for callers that want the page as a
    whole; `box` covers every table read. `read_tables` keeps them apart.
    """
    tables = await read_tables(doc, page)
    if not tables:
        return [], [], [], None
    rows = [r for t in tables for r in t.rows]
    widest = max(tables, key=lambda t: len(t.rows))
    warnings = [w for t in tables for w in t.warnings]
    boxes = [t.box for t in tables if t.box]
    box = (TableBox(page=page, source=boxes[0].source,
                    x0=min(b.x0 for b in boxes), y0=min(b.y0 for b in boxes),
                    x1=max(b.x1 for b in boxes), y1=max(b.y1 for b in boxes))
           if boxes else None)
    return rows, widest.headers, warnings, box


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
        unmapped = [h for h in unmapped if h != claimed]

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

