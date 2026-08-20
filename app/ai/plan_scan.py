"""Find the doors themselves on a floor plan, and say what type each one is.

Everything before this located the door *number* -- the little text label
printed beside a door. That is not the door. On screen the box sat on the
sticker while the actual door swing was a few millimetres away, and a number
that is missing from the drawing has no label to find at all.

So this asks a vision model to look at the plan and point at the doors.

Three decisions make it cheap and accurate, and they matter more than the
prompt does:

  where   The tags we already located bound the area worth looking at. A
          drawing sheet is mostly wall details, notes and a title block; on one
          real sheet the plan is a quarter of the page. Cropping to the doors
          we already know about throws away most of the sheet for free.

  size    Inside that crop, tiles are sized so one door is around 80 px. Sent
          the whole plan in a single picture a door is nearer 30 px, and the
          model starts inventing and missing in equal measure.

  batch   One call per tile, not one per door. A tile holds ten or twenty
          doors, so a whole project is a handful of calls rather than hundreds.

What comes back is a box and a type. What it is *not* is authoritative about
anything the schedule already states -- for a door with a tag, the schedule's
width and material win. The model is here to answer what nothing else can: is
there a door here, what shape is it, and is it missing from the schedule.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
from dataclasses import dataclass, field

from app.ai.client import AiUnavailableError, AiUpstreamError, get_client
from app.ai.pricing import BudgetExceeded, cost_usd, estimate_usd
from app.ai.response_parser import _salvage_objects, _strip_fences
from app.config import get_settings
from app.core.pdf_doc import PdfDoc

log = logging.getLogger(__name__)

# Roughly how wide a door should be in the image the model sees. Below ~50 px a
# swing arc stops being legible; far above it wastes tokens for no gain.
_DOOR_PIXELS = 80
# A 3'-0" door is about this wide in PDF points on a typical 1/8" plan. Only
# used to pick a render scale, and corrected per sheet from the real tags.
_TYPICAL_DOOR_PT = 36.0
# Tiles overlap, so a door on a seam is whole in at least one of them.
_TILE_OVERLAP = 0.10
# Longest edge of a tile in pixels. Bigger costs tokens; smaller costs tiles.
_TILE_PIXELS = 1400
# Margin around the known tags, as a fraction of their spread. Only used when
# the plan's own walls cannot be followed -- see `plan_extent`.
_REGION_PAD = 0.04
# What counts as a wall rather than a fitting, a hatch or a dimension line,
# measured in door widths so it reads the same on a 1/8" plan as a 1/4" one.
_WALL_DOORS = 4.0
# How far past the outermost door number the plan may reach, in door widths.
#
# A building's outermost doors are in its outer walls, so the plan cannot run
# far beyond them -- about one room's depth, which at these scales is six door
# widths. It is a bound, not a guess: the region is never allowed past it, so
# a stray border line cannot drag the search across the sheet.
#
# Growing outward one step at a time was tried first and does not work: there
# is open room between the last door and the outer wall -- 188 pt on one sheet
# -- so the growth stalls in the gap and stops short. Widening the step then
# lets it climb into the wall-type details stacked above the plan. A single
# bounded reach has neither failure.
_WALL_REACH_DOORS = 6.0
# Hard ceiling per request. A loop bug must not be able to run up a bill.
MAX_TILES = 40
# A first guess at the box, sized from the schedule and centred on the point.
# It is a placeholder: app/core/swing_finder.py replaces it with the measured
# extent of the door's own arc wherever there is an arc to measure. It survives
# only for sliding doors and cased openings, which have no swing to find.
_SWING_ALLOWANCE = 1.5
# Two detections closer than this fraction of the page are the same door seen
# in two overlapping tiles. Sized against a door, not the page: at 12 tiles the
# seams are more numerous than at 4, and 0.006 (about half a door) left the
# same door reported twice.
_SAME_DOOR = 0.011

_TYPES = ("single_swing", "double_swing", "double_acting", "sliding", "pocket",
          "bifold", "overhead", "opening_no_door", "not_a_door")

# The old wording led with the swing -- "a break in a wall with a straight line
# and, usually, a quarter-circle arc" -- and mentioned the rest as an
# afterthought. The model took the hint and hunted arcs. On a warehouse whose
# schedule is two thirds sectional overhead doors, it reported six of those
# sixteen; the other ten it simply passed over, because they have no arc to
# see. Sectional and roll-up doors were not named anywhere in the prompt at all.
#
# So every kind is listed, each with its own symbol, and none of them is the
# default. The sentence that matters most is the last one: no arc is still a
# door.
_WHAT_A_DOOR_IS = (
    "This image is part of an architectural floor plan. Find EVERY door and "
    "every doorway, of every kind.\n\n"
    "A door is a break in a wall. What is drawn in that break tells you which "
    "kind it is:\n"
    "- single swing: one straight panel and one quarter-circle arc\n"
    "- pair (double swing): two panels and two arcs, meeting in the middle\n"
    "- double acting: two arcs from the same hinge, one each side of the wall\n"
    "- pocket: a panel drawn inside the wall itself, often dashed. No arc\n"
    "- sliding or barn: a panel alongside the wall face, often with a track "
    "line. No arc\n"
    "- overhead (sectional, roll-up, coiling, dock or garage door): a WIDE "
    "break in the wall -- typically 8 to 16 feet -- closed by a plain line, a "
    "dashed line, or a band of short parallel strokes. Common in warehouse, "
    "industrial and loading-dock walls. No arc, no panel swinging out\n"
    "- bifold: two or more narrow folding panels\n"
    "- opening with no door: a plain gap in the wall, nothing drawn across it\n"
    "\n"
    "MOST DOORS ON SOME SHEETS HAVE NO ARC AT ALL. Do not look only for arcs. "
    "A wide gap in an exterior wall with a plain or dashed line across it is a "
    "door -- report it as overhead. Report it even if you are unsure which "
    "kind it is; say so with a lower confidence.\n\n"
    "Ignore windows, plumbing fixtures, furniture, casework, curved counters, "
    "stairs, dimension lines and anything in a circle or hexagon (those are "
    "reference bubbles, not doors).\n\n"
)

_COMMON_FIELDS = (
    f"- type: one of {', '.join(_TYPES[:-1])}\n"
    "- swing: left, right, both, or none\n"
    "- tag: the door number printed beside it, or \"\" if there is none\n"
    "- confidence: high, medium or low\n\n"
    "Report a door only where you can actually see one. If there are none, "
    "return an empty list.\n\n"
)

# Asking for a rectangle produced fabricated ones: on a real sheet the replies
# were a lattice of identical 34x36 boxes stepping down a column, every one
# "high confidence". Asking where the *centre* is asks the model for the thing
# it is actually good at -- a point of attention -- and leaves the size, which
# it has no way to know, to the schedule, which does.
_POINT_PROMPT = (
    _WHAT_A_DOOR_IS
    + "For each door report:\n"
    "- point: [x, y] as whole numbers 0-1000 measured from the top-left of "
    "THIS image, at the MIDDLE OF THE GAP IN THE WALL that this door closes. "
    "Not the middle of the room, not the door's number label, and not the far "
    "end of a swing arc. A door with no arc still has a gap -- give its "
    "middle.\n"
    + _COMMON_FIELDS
    + 'Return JSON: {"doors": [{"point": [500, 500], "type": "single_swing", '
    '"swing": "left", "tag": "101A", "confidence": "high"}]}'
)

_BOX_PROMPT = (
    _WHAT_A_DOOR_IS
    + "For each door report:\n"
    "- box: [x0, y0, x1, y1] as whole numbers 0-1000 measured from the "
    "top-left of THIS image, tight around the panel and its arc\n"
    + _COMMON_FIELDS
    + 'Return JSON: {"doors": [{"box": [0,0,0,0], "type": "single_swing", '
    '"swing": "left", "tag": "101A", "confidence": "high"}]}'
)


@dataclass(slots=True)
class DetectedDoor:
    """One door the model saw, in page fractions."""

    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    type: str
    swing: str = ""
    tag: str = ""
    confidence: str = ""

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass(slots=True)
class ScanReport:
    model: str = ""
    doors: list[DetectedDoor] = field(default_factory=list)
    tiles_planned: int = 0
    tiles_sent: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    predicted_usd: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def estimated_usd(self) -> float:
        """What this actually cost, at the rate of the model that was called.

        Previously this was hard-coded at Flash prices while the detector ran
        Pro, so every figure reported was about a quarter of the truth. The
        token counts are carried alongside precisely so the number can be
        checked against a real invoice.
        """
        return cost_usd(self.model, self.prompt_tokens, self.completion_tokens)


def region_of_interest(marks: list[tuple[float, float]], width: float,
                       height: float) -> tuple[float, float, float, float]:
    """The part of the sheet worth looking at, from the tags already located.

    Falls back to the whole page when nothing is known, which is what happens
    on a sheet whose doors are all unlabelled -- the case this module exists
    for.
    """
    if not marks:
        return (0.0, 0.0, width, height)
    xs = [m[0] * width for m in marks]
    ys = [m[1] * height for m in marks]
    pad_x = max((max(xs) - min(xs)) * _REGION_PAD, _TYPICAL_DOOR_PT * 2)
    pad_y = max((max(ys) - min(ys)) * _REGION_PAD, _TYPICAL_DOOR_PT * 2)
    return (max(0.0, min(xs) - pad_x), max(0.0, min(ys) - pad_y),
            min(width, max(xs) + pad_x), min(height, max(ys) + pad_y))


def plan_extent(doc: PdfDoc, page: int, marks: list[tuple[float, float]],
                width: float, height: float,
                door_pt: float = _TYPICAL_DOOR_PT
                ) -> tuple[float, float, float, float]:
    """The floor plan's own extent, grown from the doors out to its walls.

    Bounding the doors we already know about and adding a fixed margin is not
    the same thing, and the difference cost real doors. On one sheet the door
    numbers span 766 x 609 pt, the margin made that 910 x 753, and the plan's
    own walls run to 1052 x 860 -- so a strip 116 pt wide down the right-hand
    side, four door widths of building, was never sent to be looked at. A door
    there could not be found however good the model is.

    The plan ends where its walls end, and the drawing already says where that
    is: take the long lines lying within reach of the doors and use their
    extent. Where the plan is smaller than the reach this trims the region and
    saves tiles; where it is larger, the reach bounds it.

    Falls back to the plain margin when the sheet has no long lines to follow,
    which is what a scanned drawing looks like -- there is no vector wall in a
    photograph.
    """
    if not marks:
        return (0.0, 0.0, width, height)

    xs = [m[0] * width for m in marks]
    ys = [m[1] * height for m in marks]
    box = (min(xs), min(ys), max(xs), max(ys))

    reach = _WALL_REACH_DOORS * door_pt
    outer = (box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach)
    long_enough = _WALL_DOORS * door_pt

    # Each wall counts for the part of it that lies within reach, not for where
    # its middle happens to fall. An outer wall runs the length of the building,
    # so its midpoint can be nowhere near the doors while the wall itself passes
    # right by them.
    walls: list[tuple[float, float]] = []
    for x0, y0, x1, y1 in doc.segments(page - 1):
        if math.hypot(x1 - x0, y1 - y0) < long_enough:
            continue
        if (max(x0, x1) < outer[0] or min(x0, x1) > outer[2]
                or max(y0, y1) < outer[1] or min(y0, y1) > outer[3]):
            continue
        walls.append((min(max(x0, outer[0]), outer[2]),
                      min(max(y0, outer[1]), outer[3])))
        walls.append((min(max(x1, outer[0]), outer[2]),
                      min(max(y1, outer[1]), outer[3])))
    if not walls:
        return region_of_interest(marks, width, height)

    found = (min(box[0], min(w[0] for w in walls)),
             min(box[1], min(w[1] for w in walls)),
             max(box[2], max(w[0] for w in walls)),
             max(box[3], max(w[1] for w in walls)))

    log.info("plan_extent page=%s doors span %.0fx%.0fpt, plan runs to "
             "%.0fx%.0fpt", page, box[2] - box[0], box[3] - box[1],
             found[2] - found[0], found[3] - found[1])
    return (max(0.0, found[0]), max(0.0, found[1]),
            min(width, found[2]), min(height, found[3]))


def plan_tiles(region: tuple[float, float, float, float], door_pt: float,
               door_px: int = _DOOR_PIXELS
               ) -> tuple[list[tuple[float, float, float, float]], int]:
    """Cut the region into overlapping tiles, and say what dpi to render at.

    The dpi is chosen so a door lands near `door_px`; the tile size then follows
    from how many pixels we are willing to send. Raising `door_px` makes each
    door bigger in the picture the model sees, at the cost of more tiles -- the
    main knob on whether the model can place a door at all.
    """
    dpi = max(72, int(door_px / max(door_pt, 1.0) * 72))
    tile_pt = _TILE_PIXELS * 72 / dpi

    x0, y0, x1, y1 = region
    step = tile_pt * (1 - _TILE_OVERLAP)
    tiles: list[tuple[float, float, float, float]] = []
    y = y0
    while y < y1:
        x = x0
        while x < x1:
            tiles.append((x, y, min(x + tile_pt, x1), min(y + tile_pt, y1)))
            if x + tile_pt >= x1:
                break
            x += step
        if y + tile_pt >= y1:
            break
        y += step
    return tiles, dpi


def _cache_key(png: bytes) -> str:
    return hashlib.sha256(png).hexdigest()


def _parse_doors(content: str) -> list[dict]:
    """Doors out of the reply, surviving fences and truncation.

    Reuses the salvage the schedule tier already needed: a long reply cut off
    at the token limit still carries most of its doors.
    """
    text = _strip_fences(content)
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return [o for o in _salvage_objects(text) if "box" in o or "point" in o]
    if isinstance(parsed, dict):
        found = parsed.get("doors")
        return [d for d in found if isinstance(d, dict)] if isinstance(found, list) else []
    if isinstance(parsed, list):
        return [d for d in parsed if isinstance(d, dict)]
    return []


def _entry_box(entry: dict, tile: tuple[float, float, float, float],
               width: float, height: float, door_pt: float,
               spans: dict[str, float] | None = None
               ) -> tuple[float, float, float, float] | None:
    """One reply entry -- point or box -- as fractions of the whole page.

    A point is turned into a box of the size a door actually is, taken from the
    drawing rather than from the model. That split is the whole idea: the model
    says where to look, the schedule says how big.
    """
    tw, th = tile[2] - tile[0], tile[3] - tile[1]

    point = entry.get("point")
    if isinstance(point, (list, tuple)) and len(point) >= 2:
        try:
            px, py = (float(v) / 1000.0 for v in point[:2])
        except (TypeError, ValueError):
            return None
        if not (0.0 <= px <= 1.0 and 0.0 <= py <= 1.0):
            return None
        cx = (tile[0] + px * tw) / width
        cy = (tile[1] + py * th) / height
        # The model has no way to know how wide a door is; the schedule does.
        # A pair written "(2)3' - 0"" gets twice the box of a single, which is
        # what makes a double door look like one on screen.
        span = (spans or {}).get(str(entry.get("tag", "")).strip(), door_pt)
        span *= _SWING_ALLOWANCE
        half_x = span / 2 / width
        half_y = span / 2 / height
        return (max(0.0, cx - half_x), max(0.0, cy - half_y),
                min(1.0, cx + half_x), min(1.0, cy + half_y))

    box = entry.get("box")
    if not isinstance(box, (list, tuple)) or len(box) < 4:
        return None
    try:
        bx0, by0, bx1, by1 = (float(v) / 1000.0 for v in box[:4])
    except (TypeError, ValueError):
        return None
    bx0, bx1 = sorted((bx0, bx1))
    by0, by1 = sorted((by0, by1))
    if not (0.0 <= bx0 < bx1 <= 1.0 and 0.0 <= by0 < by1 <= 1.0):
        return None
    return ((tile[0] + bx0 * tw) / width, (tile[1] + by0 * th) / height,
            (tile[0] + bx1 * tw) / width, (tile[1] + by1 * th) / height)


def dedupe(doors: list[DetectedDoor]) -> list[DetectedDoor]:
    """Drop the same door seen twice in overlapping tiles.

    Keeps whichever the model was more sure of; on a tie, the first seen.
    """
    rank = {"high": 3, "medium": 2, "low": 1, "": 0}
    kept: list[DetectedDoor] = []
    for door in sorted(doors, key=lambda d: -rank.get(d.confidence, 0)):
        if any(math.hypot(door.cx - k.cx, door.cy - k.cy) < _SAME_DOOR
               for k in kept):
            continue
        kept.append(door)
    return kept


async def scan_sheet(doc: PdfDoc, page: int,
                     known: list[tuple[float, float]] | None = None,
                     *, door_pt: float = _TYPICAL_DOOR_PT,
                     dry_run: bool = False,
                     max_tiles: int = 0,
                     model: str | None = None,
                     ask_for: str = "point",
                     door_px: int = 0,
                     spans: dict[str, float] | None = None,
                     budget_usd: float | None = None,
                     focus_only: bool = False,
                     spent_usd: float = 0.0,
                     cache: dict[str, list[dict]] | None = None) -> ScanReport:
    """Look at one plan sheet and report the doors on it.

    `known` is the tag positions already located on this sheet, as page
    fractions -- used only to decide where to look, never to decide what is
    there. `dry_run` reports what would be sent and sends nothing.

    `model` overrides the configured one. No model is trained on CAD line
    drawings, so which to use is a question to be measured rather than argued;
    `scripts/door_bakeoff.py` scores candidates against the doors we already
    know about, and this parameter is how it does that.
    """
    settings = get_settings()
    chosen = model or settings.ai_detect_model
    prompt = _POINT_PROMPT if ask_for == "point" else _BOX_PROMPT
    door_px = door_px or settings.ai_door_pixels
    # getattr, not settings.max_tiles_per_sheet, so a server still holding an
    # older Settings falls back to the built-in cap instead of returning a 500.
    # A reloading server can pick up this module and not that one, and a hard
    # failure over a tile count is a poor trade for a stricter read.
    max_tiles = max_tiles or getattr(settings, "max_tiles_per_sheet", MAX_TILES)
    report = ScanReport(model=chosen)

    width, height = doc.page_size(page - 1)
    region = plan_extent(doc, page, known or [], width, height, door_pt)
    tiles, dpi = plan_tiles(region, door_pt, door_px)

    if focus_only and known:
        # Only look where a door is already known to be.
        #
        # This is off by default now, and the reason is worth keeping. It saved
        # about a quarter of the tiles by skipping the parts of a plan with no
        # *scheduled* door in them -- but the whole point of looking at shapes
        # is to find a door the schedule missed, and such a door is far likelier
        # to be in one of those quiet tiles than anywhere else. It was saving
        # money by declining to answer the question.
        spots = [(x * width, y * height) for x, y in known]
        tiles = [t for t in tiles
                 if any(t[0] <= sx <= t[2] and t[1] <= sy <= t[3]
                        for sx, sy in spots)]

    report.tiles_planned = len(tiles)

    if len(tiles) > max_tiles:
        report.warnings.append(
            f"page {page}: {len(tiles)} tiles needed but the cap is {max_tiles}; "
            f"scanning the first {max_tiles} only -- part of this sheet was not read"
        )
        tiles = tiles[:max_tiles]

    log.info("plan_scan page=%s region=%.0fx%.0fpt tiles=%d dpi=%d ask=%s dry_run=%s",
             page, region[2] - region[0], region[3] - region[1],
             len(tiles), dpi, ask_for, dry_run)

    if dry_run:
        # Priced from a measured per-tile rate rather than a guessed token
        # count: the guessed one was five times under on a real sheet.
        report.predicted_usd = estimate_usd(chosen, len(tiles))
        return report

    try:
        client = get_client()
    except AiUnavailableError as exc:
        report.warnings.append(f"door detection skipped: {exc}")
        return report

    for tile in tiles:
        if budget_usd is not None and spent_usd + report.estimated_usd >= budget_usd:
            report.warnings.append(
                f"page {page}: stopped at {report.tiles_sent} of {len(tiles)} "
                f"tiles -- the ${budget_usd:.2f} ceiling for this request was "
                f"reached. Part of this sheet was not looked at."
            )
            log.warning("plan_scan hit the budget ceiling on page %s", page)
            break

        png = doc.render_png(page - 1, dpi=dpi, clip=tile)
        key = _cache_key(png)
        if cache is not None and key in cache:
            found = cache[key]
        else:
            data_url = "data:image/png;base64," + base64.b64encode(png).decode()
            try:
                response = await client.chat.completions.create(
                    model=chosen,
                    temperature=0,
                    response_format={"type": "json_object"},
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }],
                )
            except Exception as exc:  # noqa: BLE001 - surface the upstream reason
                from app.ai.vision_extract import _upstream_reason
                raise AiUpstreamError(_upstream_reason(exc)) from exc

            report.tiles_sent += 1
            usage = getattr(response, "usage", None)
            if usage:
                report.prompt_tokens += usage.prompt_tokens or 0
                report.completion_tokens += usage.completion_tokens or 0
            found = _parse_doors(response.choices[0].message.content or "")
            if cache is not None:
                cache[key] = found

        for entry in found:
            kind = str(entry.get("type", "")).strip().lower()
            if kind == "not_a_door":
                continue
            mapped = _entry_box(entry, tile, width, height, door_pt, spans)
            if mapped is None:
                continue
            report.doors.append(DetectedDoor(
                page=page, x0=mapped[0], y0=mapped[1], x1=mapped[2], y1=mapped[3],
                type=kind or "unknown",
                swing=str(entry.get("swing", "")).strip().lower(),
                tag=str(entry.get("tag", "")).strip(),
                confidence=str(entry.get("confidence", "")).strip().lower(),
            ))

    report.doors = dedupe(report.doors)
    log.info("plan_scan model=%s page=%s doors=%d tiles_sent=%d tokens=%d/%d ~$%.4f",
             chosen, page, len(report.doors), report.tiles_sent,
             report.prompt_tokens, report.completion_tokens, report.estimated_usd)
    return report
