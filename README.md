# Planckoff — Door Schedule Extraction API

Takes a construction PDF, returns the door schedule as structured JSON.

```
POST  a 102-page architectural bid set (46 MB)
  ↓
GET   23 door rows as clean JSON, in ~2 seconds, using zero AI tokens
```

Standalone and stateless: PDF in, JSON out. It does not import from, modify, or
depend on the Next.js app at `planckoff-hardware`.

---

## Status

Phases 0–5 of [PLAN.md](PLAN.md) are implemented and validated against two real
bid sets. See [VALIDATION.md](VALIDATION.md) for measured results.

| Definition of done | |
|---|---|
| 46 MB Ellis County set → 23 rows in < 5 s | ✅ 2.4 s wall clock |
| `method: deterministic`, `source_pages: [21]` | ✅ |
| Zero AI tokens on that document | ✅ (runs with no API key set) |
| `pytest` green, including the golden-file test | ✅ 37 passed |
| Page finder validated against a second bid set | ✅ 2 sets — [VALIDATION.md](VALIDATION.md) |
| `/docs` renders a complete OpenAPI schema | ✅ |
| Every failure mode returns its message, not a 500 | ✅ |

---

## Setup

Python 3.11.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env
```

Run it:

```bash
uvicorn app.main:app --reload --port 8000
```

Interactive docs — and the frontend integration contract — at
<http://localhost:8000/docs>.

---

## Try it

```bash
curl -X POST http://localhost:8000/api/v1/door-schedule/extract \
  -H "X-API-Key: dev-key" \
  -F "file=@1780995376_ELLIS_COUNTY_Bid Set.pdf"
```

```json
{
  "status": "ok",
  "method": "deterministic_ruled",
  "pages_scanned": 102,
  "source_pages": [21],
  "row_count": 23,
  "duration_ms": 1962,
  "warnings": [],
  "headers": ["#", "From", "To", "Width", "Height", "Type", "Material", "Finish",
              "Frame Material", "Frame Finish", "THRESHOLD", "F_R", "HW", "Comments"],
  "rows": [
    {
      "door_tag": "22",
      "from_space": "EXTERIOR",
      "to_space": "WAREHOUSE",
      "door_width": "3' - 0\"",
      "door_height": "7' - 0\"",
      "door_type": "B",
      "door_material": "INSUL. METAL",
      "door_finish": "PAINTED",
      "frame_material": "H.M.",
      "frame_finish": "PAINTED",
      "threshold": "A570",
      "fire_rating": "0",
      "hw_set": "02",
      "comments": "FOLLOW MANU. INSTALLATION INSTRUCTIONS.",
      "extra": {}
    }
  ]
}
```

Without ever installing the service, you can run the page finder alone:

```bash
python -m app.core.page_finder "path/to/bid_set.pdf" --all
```

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness + whether the AI tier is configured |
| `POST` | `/api/v1/door-schedule/extract` | The extraction. `?debug=true` adds per-page scores, `?allow_ai=false` forbids the vision tier |
| `POST` | `/api/v1/door-schedule/inspect` | Page-finder scores only. No extraction, no AI — use it to retune thresholds on new bid sets |

All POST endpoints require the `X-API-Key` header.

### `method` — read this field first

It reports which tier produced the rows, which is the accuracy dashboard and the
token bill in one string. It is logged on every request.

| Value | Meaning |
|---|---|
| `deterministic_ruled` | Columns and rows read from the sheet's vector ruling lines. Exact, free |
| `deterministic_banded` | No rulings; columns inferred from text alignment. Free |
| `ai_vision` | Structure was unrecoverable; the candidate page went to a vision model |

Both real bid sets return `deterministic_ruled`. The AI tier has not fired yet.

---

## How it works

**Code where the structure is recoverable. AI only where it isn't.**

A door schedule in a digital PDF is a ruled table with aligned columns — fully
recoverable arithmetic. Sending it to a language model is slower, costs money,
and is less accurate. Worse, a vision model handed unreadable input invents
plausible rows rather than reporting failure, so every gate that skips the AI
path protects against silent fabrication, not just tokens.

### 1. Find the page — [`core/page_finder.py`](app/core/page_finder.py)

The hard part is not parsing the table; it is finding the one page out of 102
that holds it. Keyword scoring alone returns 17 of 102 pages — page 19 contains
the literal string "DOOR SCHEDULE" in a sheet index and no table at all.

Two structural tests cut that to exactly one page:

1. some horizontal band carries ≥ 5 distinct header words
2. ≥ 8 door-tag-like tokens share an x column below that band

Text layer only — no rendering, no AI. 102 pages in **1.4 s**.

### 2. Locate the table — [`core/table_locator.py`](app/core/table_locator.py)

Sheet A560 holds three things side by side: a hardware schedule at x < 1100, the
door schedule at 1124–2320, and the title block at x > 2330. "The page" is not
"the table", so finding the horizontal bounds is required, not an optimization.

PyMuPDF exposes `get_drawings()`, so the table's ruling lines are a **query**
rather than a heuristic. On the Ellis sheet the verticals crossing the header row
give 15 boundaries → exactly 14 columns, matching the 14 headers. This is the
main technical reason the service is Python: `pdfjs-dist` exposes no vector
geometry, which is why the TypeScript pipeline samples pixels instead.

When a sheet has no usable rulings, columns are inferred by clustering the x of
the left-aligned data — deliberately *not* from header positions, see below.

### 3. Map cells → 4. Build rows → 5. Map headers

Three traps this handles, all present on the reference sheet:

- **Headers do not sit above their data.** Headers are centre-aligned, data is
  left-aligned, and `Comments` is offset by 105 pt. Nearest-header mapping files
  every comment under `HW`. Items are assigned by column *band*, never by
  nearest anchor.
- **Wrapped cells.** `CONFERENCE` / `ROOM` is one cell on two lines. In ruled
  mode both lines are inside the same box, so this needs no heuristic at all.
- **A legitimately unnumbered row.** The first data row is an opening with no
  door number. A naive "no number ⇒ continuation" rule silently destroys it, so
  a line continues the row above only if it has no tag *and* ≤ 2 populated cells.

Header aliases (`HW` / `HDW` / `HDWE SET` / `HARDWARE GROUP` → `hw_set`) are
tested qualified-first, so `FINISH` cannot swallow `FRAME FINISH`. Headers that
map to nothing are **never dropped** — they land in the row's `extra` object.
The second bid set exercises this: its `FRAME HEAD DETAIL`, `FRAME JAMB DETAIL`,
`FRAME SILL DETAIL` and `DOOR GLAZING` columns all survive in `extra`.

### 6. AI fallback — [`ai/vision_extract.py`](app/ai/vision_extract.py)

Fires only when the deterministic tiers produce nothing. Renders the **candidate
pages only** at 200 dpi and sends them to Gemini via OpenRouter at
`temperature=0`. Never the whole document — if nothing was found and the document
is large, the API returns a clear 422 instead.

Response parsing survives markdown fences, a bare array instead of the expected
envelope, and truncation at the token limit (complete objects are recovered from
the partial JSON).

Leave `OPENROUTER_API_KEY` unset and this tier is skipped rather than failing.

---

## Errors — real messages, not 500s

A failed tier never kills the request; it records a warning and the next tier
runs. Only an all-tiers-failed state returns an error.

| Situation | HTTP | Message |
|---|---|---|
| No candidate page found | 422 | `No door schedule found — scanned 102 pages. Verify this document contains a door schedule sheet.` |
| Page found, no rows readable | 422 | `Found a door schedule on page 21 but could not read any rows.` |
| Not a PDF / corrupt | 400 | `File is not a readable PDF.` |
| Over the cap | 413 | `PDF too large (60.3 MB). Maximum is 50 MB.` |
| Missing/bad API key | 401 | `Invalid API key.` |
| OpenRouter refused | 502 | The upstream reason, surfaced as itself — a billing failure must never read as "no rows found" |

---

## Configuration

All via `.env` — see [.env.example](.env.example). Keys are never in code.

| Variable | Default | Notes |
|---|---|---|
| `API_KEY` | `dev-key` | Sent as `X-API-Key`. **Change before deploying** |
| `MAX_UPLOAD_MB` | `50` | The second bid set is 60.3 MB and needs this raised |
| `CORS_ORIGINS` | `http://localhost:3000,…` | The Next.js dev server |
| `OPENROUTER_API_KEY` | *(unset)* | Unset ⇒ AI tier skipped, not an error |
| `AI_MODEL` | `google/gemini-2.5-flash` | |
| `MIN_HEADER_HITS` / `MIN_TAG_RUN` | `5` / `8` | Page-finder gates; retune via `/inspect` |

---

## Tests

```bash
pytest
```

37 tests. The golden-file test freezes all 23 rows of page 21 field-for-field —
without it there is no way to tell an improvement from a regression.

Tests needing the full bid sets skip when the files are absent; only the 1.7 MB
single-page fixture is committed. `.gitignore` excludes `*.pdf` except
`tests/fixtures/`, because git keeps a 46 MB blob forever.

---

## Deploying

Railway or Render, via the included [Dockerfile](Dockerfile). Not Vercel — wrong
runtime, and its limits fight this workload.

```bash
docker build -t planckoff-doors .
docker run -p 8000:8000 --env-file .env planckoff-doors
```

Scale with replicas rather than in-process workers: the deterministic path is
CPU-bound and short.

---

## Deliberately not in the POC

Celery, Redis, Postgres, background workers, auth beyond a static key. The
Next.js app owns persistence. Extraction is synchronous — the deterministic path
is ~2 s and only the AI fallback approaches 30 s. The work is structured as a
single `async def extract(pdf_bytes) -> ExtractionResult`, so introducing a job
queue later means changing the route, not the logic.
