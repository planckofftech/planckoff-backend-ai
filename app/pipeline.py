"""Tier orchestration.

Try each tier in order, record which one won, and never let a failed tier kill
the request -- a failure becomes a warning and the next tier runs. Only an
all-tiers-failed state is an error.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from app.ai.client import AiUnavailableError, AiUpstreamError
from app.config import get_settings
from app.core import header_mapper, page_finder, schedule_judge
from app.core.extractor import (PageExtraction, extract_page, extract_pages,
                                is_other_schedule)
from app.core.pdf_doc import PdfDoc
from app.schemas import (
    DoorRow,
    ExtractionMethod,
    ExtractionResult,
    PageScore,
    ScheduleTable,
    TableBox,
)

log = logging.getLogger(__name__)


def _table_box(extraction) -> TableBox | None:
    """Where this table sits, as fractions of its page.

    Fractions rather than points, to match `TableBox` and `DoorLocation`
    everywhere else: the viewer draws onto an image rendered at whatever dpi it
    chooses, and a fraction is the one form that survives that.

    `source` is "text" because the box was measured off the page's own rulings
    and text, not estimated from a picture of it -- the same distinction the AI
    tier's box carries, and worth keeping where a caller may be looking at
    either.
    """
    bounds, size = extraction.bounds, extraction.page_size
    if not bounds or not size or not size[0] or not size[1]:
        return None
    x0, y0, x1, y1 = bounds
    width, height = size
    return TableBox(page=extraction.page, source="text",
                    x0=x0 / width, y0=y0 / height,
                    x1=x1 / width, y1=y1 / height)

# Which reader produced a stored reading. Raise it whenever a change means a
# set read before it would come out differently -- a stored reading is reused
# for a repeat upload only when it was made by this version (db/018).
#   2: tables found by the weighed judge and the cover-sheet index, a box
#      per table, every schedule on an image-read page read on its own.
READER_VERSION = 2

# Below this page count it is cheap enough to let the AI look at the best-
# scoring page even though no page passed the structural gates.
_SMALL_DOC_PAGES = 20
# Below this many unplaced columns the alias table is doing fine on its own.
_AI_HEADER_THRESHOLD = 2
# A scan longer than this is worth mentioning in the response.
_SLOW_SCAN_MS = 15_000
# Below either of these, a deterministic read is treated as a partial one and
# the vision tier is asked for a second opinion.
_MIN_REAL_COLUMNS = 5
_MIN_FILL_RATIO = 0.30
# Never render more than this many pages to the model, whatever the page count.
_MAX_AI_PAGES = 2
# ...and when a person asks for the image read themselves. Higher, because
# they chose to spend it, but still a ceiling: a schedule spread over more
# sheets than this is read a few sheets at a time.
_MAX_ASKED_AI_PAGES = 4


# A ruling this long, as a fraction of the page, is a table rule or a wall --
# not the hatching and symbols that make up most of a drawing's ink.
_LONG_RULE = 0.10
# How many text-less candidates to measure. Measuring is a page parse each, and
# the answer is always near the front of the architectural block.
_MAX_MEASURED = 30


def _long_rules(doc: PdfDoc, page: int) -> int:
    """How much of this page is drawn as long straight lines -- a grid, or a
    plan's walls. A schedule scores high on it even with no text at all."""
    try:
        width, height = doc.page_size(page - 1)
        lines = doc.rulings(page - 1)
    except Exception:  # noqa: BLE001 - an unreadable page simply ranks last
        return 0
    return (sum(1 for s in lines.vertical if s.length >= height * _LONG_RULE)
            + sum(1 for s in lines.horizontal if s.length >= width * _LONG_RULE))


def select_fallback_pages(doc: PdfDoc, scores: list, pages_scanned: int) -> list[int]:
    """Pages worth handing to the vision tier when nothing passed the gates.

    A scanned sheet has no text to score, so it can never pass -- it is exactly
    what the vision tier is for, and that must not be gated on document size.
    Shared with the preview endpoint so both agree on which page matters.
    """
    scanned = [c for c in scores
               if not c.has_text_layer and doc.has_raster(c.page - 1)]

    shortlist = {c.page: c for c in scanned}
    if scanned or pages_scanned <= _SMALL_DOC_PAGES:
        # Keep any page that scored at all. A scan often retains a thin text
        # layer on the sheet that matters, and that page is a far better bet
        # than the first bitmap in the file.
        shortlist.update({c.page: c for c in scores if c.score > 0})

    if not shortlist and pages_scanned <= _SMALL_DOC_PAGES:
        # Nothing scored anywhere. On a short document that means the sheet is
        # a picture carrying a title block's worth of text -- just over the
        # count that marks a page "scanned", far under anything readable. One
        # such sheet had 40 text items, none of them a heading, and was
        # reported as having no schedule without the model ever seeing it.
        shortlist.update({c.page: c for c in scores
                          if doc.has_raster(c.page - 1)})

    # A door schedule is on an architectural sheet. Where a title block names
    # another discipline the page is not a candidate, whatever is drawn on it --
    # the same rule that stops an electrical sheet's dimensions being read as
    # door numbers. On one set this removes all sixteen M/E/P sheets and leaves
    # the eleven that carry no title block at all, which are the ones we want.
    elsewhere: set[int] = set()
    try:
        from app.core import plan_index

        elsewhere = {s.page for s in plan_index.index_sheets(doc)
                     if s.number and not s.is_architectural}
    except Exception:  # noqa: BLE001 - the index is an optimisation, not a gate
        elsewhere = set()
    architectural = {p: c for p, c in shortlist.items() if p not in elsewhere}
    if architectural:
        shortlist = architectural

    # Pages with no text all score zero, so score cannot order them and they
    # came back in page order -- which handed the model the cover sheet. A
    # schedule is a grid, and a grid survives its text being flattened to
    # outlines, which is what a "scan" usually is in a drawing set.
    #
    # Long rules only. Counting every ruling ranks a plumbing sheet's pipe
    # hatching (130,000 segments) above a door schedule (19,711), which is why
    # this looked like a dead end until the disciplines were excluded above.
    # Measured on the set whose schedule is on page 11:
    #
    #     page 11  147 long rules   <- DOOR SCHEDULE
    #     page 7   141
    #     page 1    96
    measured = sorted(shortlist.values(), key=lambda c: c.page)[:_MAX_MEASURED]
    grid = {c.page: _long_rules(doc, c.page) for c in measured
            if not c.score and not c.has_text_layer}

    def rank(c) -> tuple:
        if c.score:
            return (0, -c.score, c.page)
        return (1, -grid.get(c.page, 0), c.page)

    ranked = sorted(shortlist.values(), key=rank)
    return [c.page for c in ranked[:_MAX_AI_PAGES]]


def _is_thin(extraction: PageExtraction) -> str | None:
    """Why this deterministic read looks incomplete, or None if it looks fine.

    Sheets are routinely half text and half picture: the header prints as real
    text, the rows do not. We then find the header, decide the page is
    readable, and return the two or three columns that happen to exist -- which
    looks like a complete answer and is not. One sheet returned 52 rows of 3
    columns from a table with twenty.
    """
    columns = len(extraction.headers)
    if columns < _MIN_REAL_COLUMNS:
        return f"only {columns} columns were readable as text"

    cells = filled = 0
    for row in extraction.rows:
        values = row.model_dump()
        extra = values.pop("extra", {})
        cells += columns
        filled += sum(1 for v in list(values.values()) + list(extra.values())
                      if isinstance(v, str) and v)
    if cells and filled / cells < _MIN_FILL_RATIO:
        return f"only {100 * filled / cells:.0f}% of cells held any text"
    return None


def _column_samples(extractions: list[PageExtraction], headers: list[str],
                    limit: int = 3) -> dict[str, list[str]]:
    """A few example values per unplaced column, for the header mapper.

    A heading alone cannot say whether a column identifies the row or
    classifies it -- which is how door numbers ended up in door_type.
    """
    samples: dict[str, list[str]] = {header: [] for header in headers}
    wanted = set(headers)

    for extraction in extractions:
        columns = [
            index for index, name in enumerate(extraction.headers)
            if name in wanted
        ]
        for row in extraction.rows:
            values = list(row.extra.values())
            for index in columns:
                header = extraction.headers[index]
                if len(samples[header]) >= limit:
                    continue
                key = header_mapper.extra_key(header, index)
                value = row.extra.get(key) or ""
                if value and value not in samples[header]:
                    samples[header].append(value)
            _ = values

    return {k: v for k, v in samples.items() if v}


def _index_door_pages(doc: PdfDoc) -> tuple[dict[int, str], list[str]]:
    """Pages the set's own lists point at for the door schedule, how strongly,
    and the door schedule sheets the cover index lists that are not in the file.

    The index is where an estimator looks first, and it is right far more
    often than any reading of the page: the sheet titled A6-001 SCHEDULE DOOR
    held a seven-door schedule that the eight-door gate threw away, and the
    index had named it all along.

    Two lists, cross-checked. The drawing index on the cover sheet is the
    architect's own; each sheet it lists is placed only where that page's
    title block carries the same number. The PDF's bookmarks -- or, with no
    cover index, every page's title block -- say the same thing a second way.
    """
    from app.core import drawing_index, plan_index

    pages: dict[int, str] = {}
    missing: list[str] = []
    try:
        entries = drawing_index.read_entries(doc)
        wanted = [e for e in entries
                  if schedule_judge.says_door_schedule(e.title)
                  or schedule_judge.is_schedules_sheet(e.number, e.title)]
        drawing_index.locate(doc, entries, wanted)
        for e in wanted:
            named = schedule_judge.says_door_schedule(e.title)
            if e.page:
                pages[e.page] = (schedule_judge.NAMED if named
                                 else schedule_judge.SCHEDULES)
            elif named:
                missing.append(f"{e.number} {e.title}")

        # Reading every page's title block is slow and is what the cover
        # index saves; the bookmarks cost nothing and are read either way.
        sheets = (plan_index.from_bookmarks(doc) if entries
                  else plan_index.index_sheets(doc))
        for page, level in schedule_judge.door_sheet_pages(sheets).items():
            if pages.get(page) != schedule_judge.NAMED:
                pages[page] = level
    except Exception as exc:  # noqa: BLE001 - the index helps; it is not a gate
        log.info("sheet index unreadable: %s", exc)
    return dict(sorted(pages.items())), missing


def _judged(doc: PdfDoc, candidates: list, door_pages: dict[int, str], read: set[int]
            ) -> tuple[list[tuple[int, PageExtraction]], list[tuple[int, PageExtraction]]]:
    """Read every table on these pages and sort them into found and unsure.

    Rejected tables go nowhere. The extractor's habit of handing back the
    strongest rejected table rather than nothing is exactly what this ends: a
    page whose every table fails is a page with no door schedule on it.

    `read` collects every page that yielded a table at all, judged either way:
    those pages were understood as text, so the vision tier has nothing to add.
    """
    found: list[tuple[int, PageExtraction]] = []
    unsure: list[tuple[int, PageExtraction]] = []
    if not candidates:
        return found, unsure
    for extraction in extract_pages(doc, candidates):
        if not extraction.rows:
            continue
        read.add(extraction.page)
        verdict = schedule_judge.judge(
            extraction, index_says_door=door_pages.get(extraction.page))
        if verdict.outcome == schedule_judge.FOUND:
            found.append((verdict.score, extraction))
        elif verdict.outcome == schedule_judge.UNSURE:
            unsure.append((verdict.score, extraction))
    return found, unsure


def _unread_door_sheets(door_pages: dict[int, str], accepted: set[int]) -> list[int]:
    """Sheets the index points at where reading the text gave no door schedule.

    A scanned sheet has no text at all. A CAD sheet can be subtler: one prints
    150 doors as real text and its column headings as exploded linework, so
    there is no heading row and no table to find; another sets most of its text
    as outlines, and what little is left reads as a table of nothing. Either
    way the index still says what the sheet is -- so when the text yields no
    door schedule there, the text reading failed, not the index, and the sheet
    is the one worth sending to the vision tier.

    Sheets titled as a door schedule first, then architectural schedule sheets.
    Never an unnumbered title: too weak a reason to pay for the model to look.
    """
    # A generic schedules sheet is worth a paid read only where nothing names
    # the door schedule outright -- otherwise it is a finish or toilet
    # accessory schedule bought at the price of a door schedule.
    levels = set(door_pages.values())
    wanted = ({schedule_judge.NAMED} if schedule_judge.NAMED in levels
              else {schedule_judge.SCHEDULES})
    return [p for p, level in door_pages.items()
            if level in wanted and p not in accepted]


@dataclass(slots=True)
class _ReadTable:
    """A table the image reader returned, in the shape the judge reads."""
    page: int
    title: str
    headers: list[str]
    rows: list[DoorRow]
    mapped: list[str]
    tag_guessed: bool = False


async def _read_unread_sheets(doc: PdfDoc, pages: list[int],
                              warnings: list[str], limit: int = _MAX_AI_PAGES,
                              door_pages: dict[int, str] | None = None,
                              asked: bool = False) -> list[ScheduleTable]:
    """Sheets read as images: every door schedule on each, as its own table.

    One table per printed schedule, each with its own box, so a sheet carrying
    three schedules previews as three outlined tables.

    Each is weighed by the same judge as a table read from text -- caption,
    headings, columns, values, the index -- because the model reads whatever
    table is on the page, captions included, and gets them wrong: one door
    schedule came back titled with its neighbour's GLAZING SCHEDULE caption.
    A person who named the page gets the doubtful ones too; the fallback only
    takes what the judge accepts.
    """
    door_pages = door_pages or {}
    accept = ({schedule_judge.FOUND, schedule_judge.UNSURE} if asked
              else {schedule_judge.FOUND})
    from app.ai.vision_extract import read_tables

    tables: list[ScheduleTable] = []
    for page in pages[:limit]:
        try:
            read = await read_tables(doc, page)
        except AiUpstreamError:
            raise
        except Exception as exc:  # noqa: BLE001 - a tier failure is a warning
            log.info("vision read of p%s failed: %s", page, exc)
            warnings.append(f"Page {page} could not be read.")
            continue
        for table in read:
            # Asked for every door schedule, the reader also hands back the
            # WINDOW SCHEDULE beside it -- widths and types, rows that look
            # like doors.
            fields = sorted({f for r in table.rows
                             for f, v in r.model_dump().items()
                             if v and f not in ("extra", "level")})
            verdict = schedule_judge.judge(
                _ReadTable(page, table.title, table.headers, table.rows, fields),
                index_says_door=door_pages.get(page))
            if verdict.outcome not in accept:
                continue
            # Accepted on its own headings while its caption names another
            # schedule: the caption is the neighbour's. Shown untitled, not
            # mislabelled -- a door schedule offered as GLAZING SCHEDULE.
            title = "" if is_other_schedule(table.title) else table.title
            warnings.extend(table.warnings)
            tables.append(ScheduleTable(
                title=title, page=page, headers=table.headers,
                row_count=len(table.rows), rows=table.rows, box=table.box))
    return tables


def _ai_result(tables: list[ScheduleTable], pages_scanned: int, started: float,
               warnings: list[str], page_scores: list | None = None,
               status: str = "ok") -> ExtractionResult:
    """A result read by the image reader, carrying its tables.

    Always its tables: a result with rows and no tables imports as "no
    schedule table" -- the door sheet is filled from the tables.
    """
    rows = [r for t in tables for r in t.rows]
    widest = max(tables, key=lambda t: t.row_count)
    duration = int((time.perf_counter() - started) * 1000)
    log.info("extracted method=ai_vision pages=%s tables=%s rows=%s ms=%s",
             sorted({t.page for t in tables}), len(tables), len(rows), duration)
    return ExtractionResult(
        method=ExtractionMethod.AI_VISION,
        pages_scanned=pages_scanned,
        source_pages=sorted({t.page for t in tables}),
        row_count=len(rows),
        duration_ms=duration,
        warnings=warnings,
        headers=widest.headers,
        rows=rows,
        tables=tables,
        page_scores=page_scores or [],
        box=widest.box,
        status=status,
    )


async def _asked_ai_read(doc: PdfDoc, pages: list[int], started: float,
                         warnings: list[str]) -> ExtractionResult:
    """The schedule read as images, because a person asked for it."""
    if not get_settings().ai_enabled:
        raise AiUnavailableError(
            "Image reading is not set up on this server.")
    pages = sorted({p for p in pages if 1 <= p <= doc.page_count})
    if not pages:
        raise NoScheduleFoundError(doc.page_count)
    if len(pages) > _MAX_ASKED_AI_PAGES:
        warnings.append(
            f"Read pages {', '.join(map(str, pages[:_MAX_ASKED_AI_PAGES]))}. "
            f"Ask again with the remaining pages to read them as well.")

    tables = await _read_unread_sheets(doc, pages, warnings,
                                       limit=_MAX_ASKED_AI_PAGES, asked=True)
    if not tables:
        raise NoScheduleFoundError(doc.page_count)
    return _ai_result(tables, doc.page_count, started, warnings)


@dataclass(slots=True)
class Detection:
    """Where the door schedule is, before anything is made of it.

    The one answer to "which tables are the door schedule" -- the schedule
    read and the plan audit both ask it, and they must not answer it two ways.
    """
    door_pages: dict[int, str]
    missing: list[str]
    read: set[int]
    found: list[tuple[int, PageExtraction]]
    unsure: list[tuple[int, PageExtraction]]
    scores: list
    candidates: list


def detect(doc: PdfDoc, *, debug: bool = False) -> Detection:
    """Find the door schedule tables: the sheet index first, then every page.

    Nearly every set names its door schedule sheet. Those pages are read
    whatever they score -- a seven-door schedule is still a schedule -- and
    when they hold the door schedule, the other 600 pages are not searched.
    """
    settings = get_settings()
    door_pages, missing = _index_door_pages(doc)
    named = [page_finder.score_page(doc.text_items(p - 1), p,
                                    min_header_hits=settings.min_header_hits,
                                    min_tag_run=settings.min_tag_run)
             for p in door_pages]
    read: set[int] = set()
    found, unsure = _judged(doc, named, door_pages, read)

    scores: list = []
    candidates: list = []
    if not found or debug:
        scores = page_finder.find_schedule_pages(
            doc,
            min_header_hits=settings.min_header_hits,
            min_tag_run=settings.min_tag_run,
        )
        candidates = page_finder.passing(scores)
    if not found:
        more_found, more_unsure = _judged(
            doc, [c for c in candidates if c.page not in door_pages],
            door_pages, read)
        found += more_found
        unsure += more_unsure
    return Detection(door_pages, missing, read, found, unsure, scores, candidates)


class NoScheduleFoundError(RuntimeError):
    def __init__(self, pages_scanned: int):
        self.pages_scanned = pages_scanned
        super().__init__(
            f"No door schedule found - scanned {pages_scanned} pages. "
            "Verify this document contains a door schedule sheet."
        )


class NoRowsError(RuntimeError):
    def __init__(self, pages: list[int], pages_scanned: int):
        self.pages = pages
        self.pages_scanned = pages_scanned
        page_list = ", ".join(str(p) for p in pages)
        super().__init__(
            f"Found a door schedule on page {page_list} but could not read any rows."
        )


def _levels_from_titles(tables: list[ScheduleTable]) -> None:
    """Name each table's doors by what its caption says and the others do not.

    A set that splits its doors by storey does it one of two ways. It prints a
    band across the table -- "LEVEL 08" -- which the extractor reads directly;
    or it prints one table per storey and says so in the caption:

        ARCHITECTURE- DOOR SCHEDULE_LEVEL 1
        ARCHITECTURE- DOOR SCHEDULE_LEVEL 5
        ARCHITECTURE- DOOR SCHEDULE_GARAGE
        ARCHITECTURE- DOOR SCHEDULE_UNITS

    What every caption shares is the name of the schedule; what one caption has
    and the rest do not is the part of the building it covers. So the shared
    opening is dropped and the remainder is the level, without a list of storey
    words to maintain -- which matters, because GARAGE and UNITS are neither
    storeys nor spellable in advance, and they divide the doors just the same.

    Only where a table has no level already: a band states it per row and is
    the better evidence. And only where the captions genuinely differ -- one
    set heads both of its schedules "DOOR FRAME", which distinguishes nothing.
    """
    titled = [t for t in tables if t.title.strip()]
    if len(titled) < 2:
        return
    titles = [t.title.strip() for t in titled]
    if len(set(titles)) < 2:
        return
    shared = 0
    for position, letters in enumerate(zip(*titles)):
        if len(set(letters)) > 1:
            break
        shared = position + 1
    # A caption that runs out exactly where they diverge is the same schedule
    # carried on, not a part of the building: one set heads its pages "DOOR
    # SCHEDULE" and "DOOR SCHEDULE CONTINUE.". Checked before the back-off
    # below, which hands a whole shared word back and so leaves both remainders
    # looking non-empty -- "SCHEDULE" and "SCHEDULE CONTINUE.", neither a place.
    if shared == min(len(t) for t in titles):
        return

    # Back off to a separator, or a caption is cut mid-word and the level comes
    # back as "EVEL 1". An underscore counts: one set writes its captions
    # "ARCHITECTURE- DOOR SCHEDULE_LEVEL 1", so the part that differs begins
    # after one, and backing off to the last space alone left "SCHEDULE_" on
    # the front of every storey.
    opening = titles[0][:shared]
    cut = max(opening.rfind(c) for c in " _-:")
    shared = cut + 1 if cut >= 0 else 0

    # Keep backing off while what is left says nothing. Two captions reading
    # "..._LEVEL 1" and "..._LEVEL 5" share everything up to the number, so the
    # difference between them is "1" and "5" -- true, and no use to anyone
    # reading a door's level. Give back the word that makes it "LEVEL 1".
    while shared > 0 and not any(
            any(ch.isalpha() for ch in t[shared:]) for t in titles):
        cut = max(opening[:shared - 1].rfind(c) for c in " _-:")
        shared = cut + 1 if cut >= 0 else 0

    names = [t.title.strip()[shared:].strip(" _-:") for t in titled]
    # A caption with nothing left once the shared opening is removed is the
    # same schedule carried on, not a part of the building: one set heads its
    # pages "DOOR SCHEDULE" and "DOOR SCHEDULE CONTINUE.", which would divide
    # its doors into no level at all and "CONTINUE.". Neither is a place, so
    # the whole derivation is abandoned rather than applied to one page.
    if not all(names):
        return

    for table, name in zip(titled, names):
        if name == table.title.strip():
            continue
        for row in table.rows:
            if not row.level:
                row.level = name


async def extract(source: bytes | str | Path, *, allow_ai: bool = True,
                  debug: bool = False, ai_read: bool = False,
                  pages: list[int] | None = None) -> ExtractionResult:
    """PDF in, JSON out. Stateless.

    `source` is bytes or a path. A path keeps memory flat on a large set --
    see PdfDoc.

    Two ways to use the image reader, and they are different requests:

    `allow_ai` -- the fallback. It steps in only where the text could not be
    read, and only on the pages that need it.

    `ai_read` -- asked for. A person looking at a schedule that came back
    wrong, or not at all, wants it read as an image. `pages` names the sheets
    when they know them; otherwise the door schedule sheets found here are
    read.

    Kept async so swapping in a job queue later means changing the route, not
    the logic.
    """
    settings = get_settings()
    started = time.perf_counter()
    warnings: list[str] = []

    with PdfDoc(source) as doc:
        pages_scanned = doc.page_count

        # Told where to look: no searching at all.
        if ai_read and pages:
            return await _asked_ai_read(doc, pages, started, warnings)

        found_where = detect(doc, debug=debug)
        door_pages, read = found_where.door_pages, found_where.read
        # The architect lists a door schedule this file does not hold --
        # another volume, or a sheet left out of the upload. Worth knowing
        # before anyone prices from what is here.
        for listed in found_where.missing:
            warnings.append(f"The drawing index lists {listed}, but that "
                            "sheet is not in this file.")
        judged_found, judged_unsure = found_where.found, found_where.unsure
        accepted_pages = {e.page for _, e in judged_found + judged_unsure}
        scores, candidates = found_where.scores, found_where.candidates
        scan_ms = int((time.perf_counter() - started) * 1000)
        log.info("detect pages=%s found=%s unsure=%s ms=%s", pages_scanned,
                 sorted({e.page for _, e in judged_found}),
                 sorted({e.page for _, e in judged_unsure}), scan_ms)

        # A large set is slow whatever we do. Say so, so a long wait is visible
        # before a demo rather than during one.
        if scan_ms > _SLOW_SCAN_MS:
            warnings.append(
                f"Scanning {pages_scanned} pages took {scan_ms / 1000:.0f} s. "
                "Large drawing sets are slow to read; this is not a fault."
            )

        page_scores = [PageScore(**c.as_dict()) for c in scores] if debug else []

        # --- Tier 1: deterministic ------------------------------------------
        # Found tables only, when there are any. Unsure ones are returned only
        # in their absence, and the result says so -- "is this your door
        # schedule?" is a better answer than either a guess or nothing.
        if ai_read:
            located = sorted({e.page for _, e in judged_found + judged_unsure}
                             | set(_unread_door_sheets(door_pages, accepted_pages)))
            return await _asked_ai_read(
                doc, located or [c.page for c in candidates], started, warnings)

        status = "ok"
        accepted = judged_found
        if not accepted and judged_unsure:
            accepted, status = judged_unsure, "unsure"
        found: list[PageExtraction] = [e for _, e in accepted]
        best = (max(accepted, key=lambda s: (s[0], len(s[1].rows)))[1]
                if accepted else None)

        if best is not None and best.rows:
            # Columns the alias table could not place. No firm names them the
            # same way, so resolve the leftovers instead of shipping them as
            # extras -- headers only, never the table. Pooled across every
            # schedule on the sheet: they share a vocabulary, so one call
            # answers for all of them, and resolving only the largest left the
            # rest with the same columns filed under `extra`.
            leftovers: list[str] = []
            for extraction in found:
                for header in extraction.unmapped:
                    if header not in leftovers:
                        leftovers.append(header)

            if (len(leftovers) >= _AI_HEADER_THRESHOLD and allow_ai
                    and settings.ai_enabled):
                from app.ai.header_map import resolve_headers

                samples = _column_samples(found, leftovers)
                overrides, hint_warnings = await resolve_headers(leftovers, samples)
                warnings.extend(hint_warnings)
                if overrides:
                    refreshed = []
                    for extraction in found:
                        if extraction.unmapped and extraction.candidate is not None:
                            retried = extract_page(doc, extraction.candidate,
                                                   overrides)
                            refreshed.append(retried if retried.rows else extraction)
                        else:
                            refreshed.append(extraction)
                    found = refreshed
                    best = max(found, key=lambda e: len(e.rows))
            warnings.extend(best.warnings)

            # A partial read looks like a complete answer, so check before
            # trusting it. The vision tier only wins if it actually understands
            # more of the table -- otherwise the measured read stands.
            thin = _is_thin(best)
            if thin and allow_ai and settings.ai_enabled:
                # Every table read off the page must describe doors -- more
                # columns is not the same as the right table. This branch
                # re-reads a page whose text layer looked thin, and on a set
                # whose architectural sheets are all flattened to outlines the
                # only page with any text was an electrical one: the re-read
                # came back with its LIGHT FIXTURE SCHEDULE, nine columns wide,
                # and nine beat the four we had. It won on width alone.
                reread: list[ScheduleTable] = []
                reread_warnings: list[str] = []
                try:
                    reread = await _read_unread_sheets(doc, [best.page],
                                                       reread_warnings,
                                                       door_pages=door_pages)
                except AiUpstreamError:
                    raise
                if reread and max(len(t.headers) for t in reread) > len(best.headers):
                    warnings.extend(reread_warnings)
                    return _ai_result(reread, pages_scanned, started, warnings,
                                      page_scores, status)

            tables = [
                ScheduleTable(title=e.title, page=e.page, headers=e.headers,
                              field_map=e.mapped, row_count=len(e.rows),
                              rows=e.rows, box=_table_box(e))
                for e in sorted(found, key=lambda e: (e.page, -len(e.rows)))
            ]
            # The index named more door schedule sheets than the text tier
            # could read. One set names A5.12, CR1.04 and CR1.05; the last two
            # read as text, and A5.12 -- three schedules, 150 doors, headings
            # drawn as linework -- was dropped without a word. Read the rest as
            # images and add them, rather than returning two thirds of a set.
            if allow_ai and settings.ai_enabled:
                tables += await _read_unread_sheets(
                    doc, _unread_door_sheets(door_pages, accepted_pages), warnings,
                    door_pages=door_pages)
            _levels_from_titles(tables)
            # Every door on the sheet, not the biggest table's doors.
            #
            # `rows` used to be the winning table alone. A sheet with two
            # schedules printed side by side -- doors 101-117 on the left,
            # 118-126 on the right -- reported fifteen doors out of twenty-five,
            # and everything downstream inherited the loss: the master sheet,
            # the plan audit, the count on screen. The other tables were right
            # there in `tables` and simply never added up.
            all_rows = [r for t in tables for r in t.rows]
            if len(tables) > 1:
                warnings.append(
                    f"{len(tables)} door schedules were read: "
                    + ", ".join(f"{t.title or 'untitled'} (page {t.page}, "
                                f"{t.row_count} doors)" for t in tables)
                )
            duration = int((time.perf_counter() - started) * 1000)
            log.info("extracted method=%s page=%s tables=%s rows=%s ms=%s",
                     best.method.value, best.page, len(tables), len(all_rows),
                     duration)
            return ExtractionResult(
                method=best.method,
                pages_scanned=pages_scanned,
                source_pages=sorted({t.page for t in tables}),
                row_count=len(all_rows),
                duration_ms=duration,
                warnings=warnings,
                headers=best.headers,
                rows=all_rows,
                tables=tables,
                page_scores=page_scores,
                status=status,
            )

        # --- Tier 2: AI vision, candidate pages only -------------------------
        # The index's own door schedule sheets first, where they could not be
        # read as text. Then candidates whose text gave no table at all. Never
        # a page whose tables were read and judged not to be doors -- the model
        # would only read the same piping schedule again, at a price.
        ai_pages = (_unread_door_sheets(door_pages, accepted_pages)
                    or [c.page for c in candidates if c.page not in read][:_MAX_AI_PAGES])
        ai_pages = ai_pages[:_MAX_AI_PAGES]

        # Nothing passed the gates. On a small document the best-scoring page is
        # still worth a look, but that is a guess, not a find -- and it must not
        # be reported as one. `speculative` keeps the two apart so the error
        # message never claims a schedule was located when it was not.
        speculative = False
        if not ai_pages:
            scanned = [c for c in scores
                       if not c.has_text_layer and doc.has_raster(c.page - 1)]
            ai_pages = select_fallback_pages(doc, scores, pages_scanned)
            speculative = bool(ai_pages) and not scanned


        # A candidate whose tables were read and judged not to be doors is not
        # a door schedule we failed to read -- it is no door schedule at all.
        unread = [c.page for c in candidates if c.page not in read]

        def _give_up() -> RuntimeError:
            if speculative or not unread:
                return NoScheduleFoundError(pages_scanned)
            return NoRowsError(unread, pages_scanned)

        if not ai_pages:
            raise NoScheduleFoundError(pages_scanned)

        if not allow_ai:
            raise _give_up()

        if not settings.ai_enabled:
            warnings.append(
                "This drawing set needs image reading, which is not set up on "
                "this server."
            )
            raise _give_up()

        tables = await _read_unread_sheets(doc, ai_pages, warnings,
                                           door_pages=door_pages)
        if tables:
            return _ai_result(tables, pages_scanned, started, warnings,
                              page_scores)

        raise _give_up()

