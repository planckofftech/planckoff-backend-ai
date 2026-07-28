# Door Schedule Extraction API — POC Implementation Plan

> **Read this file completely before writing any code.**
> It contains findings already validated against a real 102-page construction
> bid set. Do not re-derive them. Do not skip to Phase 4.

---

## 0. Mission

Build a **standalone Python service** that takes a construction PDF and returns
the door schedule as structured JSON.

```
POST  a 102-page architectural bid set (46 MB)
  ↓
GET   22 door rows as clean JSON, in ~3 seconds, using zero AI tokens
```

The hard part is **not** parsing the table. The hard part is finding the one
page out of 102 that holds the schedule, without sending the whole document to
a vision model.

This service is **completely independent** of the existing Next.js app at
`c:\planckoff\planckoff-hardware`. Do not import from it, do not modify it, do
not read its source for anything except the reference notes in §9.

---

## 1. Non-negotiable design principle

**Code where the structure is recoverable. AI only where it isn't.**

A door schedule in a digital PDF is a ruled table with aligned columns. That is
*fully recoverable arithmetic* — text positions, column bands, row clustering.
Sending it to a language model is slower, costs money, and is less accurate
than doing the arithmetic.

AI is the fallback for the cases where structure genuinely went missing:

1. No text layer at all (scanned sheet)
2. Text layer exists but the schedule is a pasted raster image
3. Broken font encoding — text extracts as garbage glyphs
4. Columns don't align (merged cells, hand-built tables)

The test is never "is this PDF digital." The test is **"can code recover the
table structure from it."**

A critical failure mode to design against: **a vision model handed unreadable
input will invent plausible-looking rows rather than report failure.** Every
gate that skips the AI path is protecting you from silent fabrication, not just
saving tokens.

---

## 2. Validated findings — DO NOT RE-DERIVE

All of this was measured on `1780995376_ELLIS_COUNTY_Bid Set.pdf`
(102 pages, 46 MB, currently at `c:\planckoff\planckoff-hardware\`).

### 2.1 The target page

**Page 21** = sheet **A560, "DOOR / HARDWARE / SCHEDULE"**.
Page size 2592 × 1728 pt (36" × 24" arch E1), rotation 0, 485 non-empty text
items. It has a clean text layer.

The sheet holds **three separate things side by side**:

| Region | x range (pt, scale 1) | Content |
|---|---|---|
| Left | ~40 – 1100 | HARDWARE SCHEDULE, groups 01–14, two sub-columns |
| **Middle** | **1124 – 2320** | **Door Schedule ← the target** |
| Right | 2330+ | Title block (firm name, address, sheet number, dates) |

**Consequence:** you cannot treat "the page" as "the table." Locating the
table's horizontal bounds is a required step, not an optimization.

### 2.2 Column anchors (measured)

Header row is at **y = 125**. Headers are *centre-aligned*, data is
*left-aligned*, so **header x and data x differ by up to 45 pt**:

| Column | Header x | Data x |
|---|---|---|
| `#` | 1132 | 1124 |
| `From` | 1182 | 1151 |
| `To` | 1285 | 1247 |
| `Width` | 1349 | 1342 |
| `Height` | 1396 | 1393 |
| `Type` | 1446 | 1441 |
| `Material` | 1526 | 1483 |
| `Finish` | 1636 | 1620 |
| `Frame Material` | 1705 | 1692 |
| `Frame Finish` | 1825 | 1809 |
| `THRESHOLD` | 1884 | 1881 |
| `F.R` (extracts as `F_R`) | 1972 | 1969 |
| `HW` | 2008 | 2004 |
| `Comments` | 2144 | 2039 |

> **Trap:** never map a cell by "nearest header x." `Comments` is off by 105 pt.
> Build **bands** from midpoints between consecutive anchors, then assign each
> text item to the band containing its x. See §5.3.

### 2.3 The page finder works — prototype already validated

Keyword matching **alone is useless**. Scoring pages by presence of
`DOOR SCHEDULE`, `WIDTH`, `HEIGHT`, `FRAME`, `THRESHOLD`, etc. returned
**17 of 102 pages**. Page 19 even contains the literal string "DOOR SCHEDULE"
(a sheet index) while containing no table.

Adding **two structural tests** isolates the page exactly:

1. Some horizontal line (y-band, ±5 pt) carries **≥ 5 distinct header words**
2. **≥ 8 door-tag-like tokens** share an x column (±6 pt) *below* that line

Measured result on the real file:

```
structural scan of all 102 pages: 2797 ms   (no rendering, no AI)
PASSING PAGES:
[{ page: 21, header_hits: 12, header_y: 125, tag_run: 22, tag_x: 1124 }]
```

`tag_run = 22` matches the 22 door rows exactly. **One page, correct page,
2.8 seconds, zero tokens.** Port this algorithm faithfully — it is the single
highest-value component in the project.

Reference implementation (JavaScript prototype, port to PyMuPDF):

```
for each page:
    items = text items with viewport x/y, non-empty       # skip if < 40 items
    # 1. header line
    bucket items by round(y / 5)
    for each bucket: count distinct HEADER_WORDS matching (equality or prefix)
    header_hits, header_y = the best bucket
    # 2. tag column below the header
    tags = items matching TAG_RE and y > header_y
    cluster tags by x with tolerance 6 pt
    tag_run = size of the largest cluster
    # 3. verdict
    pass if header_hits >= 5 and tag_run >= 8
    score = header_hits * 2 + min(tag_run, 30)
```

```python
TAG_RE = re.compile(r'^(?=.*\d)[A-Z0-9]{1,6}(?:[-.][A-Z0-9]{1,4})?$', re.I)

HEADER_WORDS = [
    'WIDTH', 'HEIGHT', 'TYPE', 'MATERIAL', 'FINISH', 'FRAME', 'THRESHOLD',
    'RATING', 'F.R', 'F_R', 'HW', 'HARDWARE', 'COMMENTS', 'REMARKS', 'MARK',
    'DOOR NO', 'FROM', 'TO', 'GLAZ', 'LOUVER', 'SIZE', 'THK',
]
```

> The thresholds `header_hits >= 5` and `tag_run >= 8` are fitted to **one
> document**. Log both scores for every page on every run so they can be
> retuned from real data instead of guessed. See §8.

---

## 3. Tech stack

| Layer | Choice | Why this and not the alternative |
|---|---|---|
| Language | **Python 3.11** | 3.11.9 confirmed installed. Best PyMuPDF wheel support |
| API | **FastAPI** + **uvicorn** | Async uploads; auto-generated OpenAPI at `/docs` becomes the frontend integration contract |
| Validation | **Pydantic v2** | The response schema *is* the type — one source of truth shared with the TS frontend |
| PDF engine | **PyMuPDF (`fitz`)** | Text+bbox, **vector lines**, and rendering from one C-speed library |
| Table assist | **pdfplumber** | `extract_tables()` with explicit-line strategy; fallback when custom clustering struggles |
| AI fallback | **openai** SDK → OpenRouter | Same provider/model the existing app uses. Do **not** add a second provider |
| Config | **pydantic-settings** | Keys in `.env`, never in code |
| Tests | **pytest** | Golden-file regression. Non-optional — see §8 |
| Deploy | **Railway** or **Render** (Docker) | Not Vercel — wrong runtime, and its limits fight this workload |

### Why PyMuPDF specifically

The existing TypeScript pipeline uses `pdfjs-dist`, which exposes text
positions but **no vector geometry**. To find out whether a checkbox is ticked
it must render the page to pixels and sample dark-pixel fractions — ~640 lines
of workaround. It also depends on `@napi-rs/canvas`, a native binary that does
not load reliably on serverless.

PyMuPDF gives you all three layers directly:

```python
page.get_text("dict")     # spans with exact bbox
page.get_drawings()       # every vector line — table rulings, as data
page.get_pixmap(dpi=200)  # rendering, no native-binary roulette
```

Table ruling lines become a **query**, not a heuristic. This is the main
technical reason to build the service in Python rather than extending the
existing TypeScript.

### Explicitly NOT in the POC

Celery, Redis, Postgres, Docker Compose, background workers, auth beyond a
static key. The existing Next.js app owns persistence. **This service is
stateless: PDF in, JSON out.**

---

## 4. Repository layout

Standalone git repo at `c:\planckoff\planckoff-backend-ai\`.

```
planckoff-backend-ai/
├── PLAN.md                     # this file
├── README.md
├── requirements.txt
├── Dockerfile
├── .env.example
├── .gitignore                  # MUST ignore *.pdf except tests/fixtures/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app, CORS, router mount
│   ├── config.py               # pydantic-settings
│   ├── schemas.py              # DoorRow, ExtractionResult, ExtractionMethod
│   ├── pipeline.py             # orchestrator — tier order, method tracking
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes.py           # the two endpoints
│   │   └── deps.py             # API-key dependency
│   ├── core/
│   │   ├── __init__.py
│   │   ├── pdf_doc.py          # PyMuPDF wrapper: open, positioned text, render
│   │   ├── page_finder.py      # Phase 1 — which pages hold a schedule
│   │   ├── table_locator.py    # Phase 2a — bounds + column anchors
│   │   ├── cell_mapper.py      # Phase 2b — items → cells
│   │   ├── row_builder.py      # Phase 2c — continuation stitching
│   │   └── header_mapper.py    # Phase 3 — headers → canonical fields
│   └── ai/
│       ├── __init__.py
│       ├── client.py           # OpenRouter client
│       └── vision_extract.py   # Phase 4 — render page → Gemini
└── tests/
    ├── fixtures/
    │   ├── ellis_p21.pdf       # page 21 alone, ~2 MB — COMMIT THIS
    │   └── ellis_p21_expected.json   # golden ground truth — COMMIT THIS
    ├── test_page_finder.py
    ├── test_row_builder.py
    └── test_pipeline.py
```

### Creating the test fixture

Never commit the 46 MB source PDF — git keeps it forever.

```python
import fitz
src = fitz.open(r"c:\planckoff\planckoff-hardware\1780995376_ELLIS_COUNTY_Bid Set.pdf")
out = fitz.open()
out.insert_pdf(src, from_page=20, to_page=20)   # 0-indexed → page 21
out.save("tests/fixtures/ellis_p21.pdf")
```

`.gitignore` must contain:
```
*.pdf
!tests/fixtures/*.pdf
```

---

## 5. Implementation phases

Each phase ends in something runnable and checkable. **Do not start a phase
before the previous one passes its acceptance criteria.**

### Phase 0 — Skeleton (half day)

FastAPI app; `POST /api/v1/door-schedule/extract` accepts a multipart upload
and returns `{"page_count": 102, "size_mb": 46.2}`. `GET /health` returns ok.

**Acceptance:** upload the real 46 MB PDF, get `page_count: 102` back.
Proves plumbing (upload limits, memory, PyMuPDF install) before any real logic.

---

### Phase 1 — Page finder (1 day) ← highest value

Port §2.3 to PyMuPDF in `core/page_finder.py`.

```python
@dataclass
class PageCandidate:
    page: int          # 1-indexed
    header_hits: int
    header_y: float
    tag_run: int
    tag_x: float
    score: int
```

Ship a CLI alongside the API:

```bash
python -m app.core.page_finder "path/to/bid_set.pdf"
→ scanned 102 pages in 2.8s
→ page 21   header_hits=12  tag_run=22  tag_x=1124  score=46
```

**Real problem this solves:** a vision model cannot be handed 102 pages. This
is the difference between ~$0.001 and several dollars per document, and
between 3 seconds and several minutes.

**Acceptance:**
- Returns exactly `[21]` for the Ellis County set
- Completes in < 5 s
- Zero AI calls
- Logs per-page scores for every page, not just winners

---

### Phase 2 — Deterministic extraction (2–3 days)

The core. Three modules.

#### 5.1 `table_locator.py`
Given a candidate page: take the header row identified in Phase 1; its text
items' x-span defines the table's left/right bounds. **This is what excludes
the hardware schedule at x<1100 and the title block at x>2330.** Walk down from
`header_y` collecting rows until they stop.

Cross-check bounds against `page.get_drawings()` vertical lines when available —
ruling lines are more reliable than text extents, and PyMuPDF gives them free.

Output: bounds + ordered list of `(header_text, x)` anchors.

#### 5.2 `cell_mapper.py`
Build **bands** from anchors: band *i* spans the midpoint between anchor *i-1*
and *i* to the midpoint between *i* and *i+1*. Assign each text item to the
band containing its x. **Do not use nearest-anchor** — see the §2.2 trap.

Cluster items into rows by y, tolerance ≈ 0.6 × median font height (≈4 pt here;
verify against `span["size"]`, don't hardcode).

#### 5.3 `row_builder.py` — the subtle part

Two failure modes, both present on page 21:

**Wrapped cells.** `To` = `CONFERENCE` / `ROOM` on two lines; `Comments` =
`FOLLOW MANU. INSTALLATION` / `INSTRUCTIONS.` on two lines. These must be
joined with a single space into the previous row's cell.

**A legitimately unnumbered row.** The first data row is
`RECEPTION | HALL | 8'-0" | 7'-0" | OPEN | ... | OPENING` with **no value in
the `#` column** — it's an opening with no door number. A naive rule of
"no number ⇒ continuation" silently destroys it.

**The rule:**

```
a row is a CONTINUATION if:
    it has no value in the tag column
    AND it has <= 2 populated columns
otherwise it is a NEW ROW (even with an empty tag column)
```

**Acceptance:** `ellis_p21.pdf` → **exactly 23 rows** (1 unnumbered + 22
numbered), matching `ellis_p21_expected.json` field-for-field. Freeze as a
pytest golden test.

---

### Phase 3 — Header mapping (half day)

`core/header_mapper.py`. Alias table → canonical field names:

```python
HEADER_ALIASES = {
    'door_tag':       ['#', 'NO', 'NO.', 'MARK', 'DOOR NO', 'DOOR NO.', 'DOOR #', 'TAG'],
    'from_space':     ['FROM'],
    'to_space':       ['TO'],
    'door_width':     ['WIDTH', 'W', 'DOOR WIDTH'],
    'door_height':    ['HEIGHT', 'HT', 'H', 'DOOR HEIGHT'],
    'door_type':      ['TYPE', 'DOOR TYPE', 'DR TYPE'],
    'door_material':  ['MATERIAL', 'DOOR MATERIAL', 'MATL'],
    'door_finish':    ['FINISH', 'DOOR FINISH'],
    'frame_material': ['FRAME MATERIAL', 'FRAME MATL', 'FRM MATERIAL'],
    'frame_finish':   ['FRAME FINISH', 'FRM FINISH'],
    'threshold':      ['THRESHOLD', 'THRESH'],
    'fire_rating':    ['F.R', 'F_R', 'FR', 'RATING', 'FIRE RATING', 'LABEL'],
    'hw_set':         ['HW', 'HDW', 'HDWE', 'HW SET', 'HARDWARE', 'HARDWARE SET',
                       'HARDWARE GROUP', 'HDW SET'],
    'comments':       ['COMMENTS', 'REMARKS', 'NOTES'],
}
```

Ordering matters: `FRAME FINISH` must be tested before `FINISH`, and
`FRAME MATERIAL` before `MATERIAL`, or the bare alias swallows the qualified one.

**Unrecognized headers must never be dropped** — put them in an `extra: dict`
on the row.

If **≥ 2** headers are unmapped, make one small AI call sending *only the header
strings* (~40 tokens) asking for a mapping to canonical names. Cache the result
per header-signature.

**Real problem this solves:** every architecture firm names columns differently
— `HW` / `HDW` / `HDWE SET` / `HARDWARE GROUP` all mean the same thing. Without
this, the extractor works on exactly one firm's drawings.

---

### Phase 4 — AI fallback (1 day)

Triggers when, for a candidate page:
- the page has no usable text layer, **or**
- Phase 2 produced 0 rows, **or**
- `header_hits < 5` on every page but the document is small (≤ 20 pages)

`page.get_pixmap(dpi=200)` on the **candidate pages only** → PNG → base64 →
Gemini via OpenRouter, with the Pydantic schema as the `json_schema` response
format, `temperature=0`.

**Never send the whole document.** If the page finder found nothing and the
document is large, return a clear error instead — see §7.

Parsing must survive: markdown code fences, a bare array instead of the
expected envelope, and truncation at the token limit (recover complete objects
before the cut-off).

---

### Phase 5 — Hardening + UI contract (1 day)

- CORS allowing `http://localhost:3000` (the Next.js dev server)
- `X-API-Key` header checked by a FastAPI dependency
- 50 MB upload cap, rejected with 413 and a readable message
- Structured logging: page count, chosen method, duration, token cost
- Hand `/docs` to whoever wires the frontend

**Total: ~1 week to a working POC.**

---

## 6. API contract

### `POST /api/v1/door-schedule/extract`
`multipart/form-data`, field `file`. Header `X-API-Key`.

```json
{
  "status": "ok",
  "method": "deterministic",
  "pages_scanned": 102,
  "source_pages": [21],
  "row_count": 23,
  "duration_ms": 3140,
  "warnings": [],
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

`method` is `"deterministic" | "ai_vision" | "ai_text"`.

> **`method` is the most important field in the POC.** It tells you, per
> document, which path fired — that is your accuracy dashboard and your token
> bill in one string. Log it on every request.

### `GET /health`
```json
{"status": "ok", "version": "0.1.0"}
```

### Sync vs async

Start **synchronous**. The deterministic path is ~3 s; only the AI fallback
approaches 30 s. Structure the work as a single
`async def extract(pdf_bytes) -> ExtractionResult` so swapping in a job queue
later means changing the route, not the logic. **Do not build a queue now.**

---

## 7. Error handling — real messages, not 500s

| Situation | HTTP | Message |
|---|---|---|
| No candidate page found | 422 | `"No door schedule found — scanned 102 pages. Verify this document contains a door schedule sheet."` |
| Page found, 0 rows, AI also failed | 422 | `"Found a door schedule on page 21 but could not read any rows."` |
| Not a PDF / corrupt | 400 | `"File is not a readable PDF."` |
| > 50 MB | 413 | `"PDF too large (62.1 MB). Maximum is 50 MB."` |
| Missing/bad API key | 401 | `"Invalid API key."` |
| OpenRouter 402/401 | 502 | Surface the upstream reason — never let a billing failure surface as "no rows found" |

A failed tier must never kill the request — record a warning and continue to
the next tier. Only an all-tiers-failed state returns an error.

---

## 8. Validation — do this, it is not optional

### 8.1 Golden fixture

Before Phase 2 is "done", hand-verify the 23 rows of page 21 into
`tests/fixtures/ellis_p21_expected.json`. **Ground truth, already read off the
PDF's text layer** — transcribe it, then spot-check against the rendered page:

| # | From | To | W | H | Type | Material | Finish | Frame Matl | Frame Fin | Thresh | F.R | HW | Comments |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| — | RECEPTION | HALL | 8'-0" | 7'-0" | | OPEN | | | | | | | OPENING |
| 1 | RECEPTION | EXTERIOR | 6'-0" | 8'-0" | A | STOREFRONT | | H.M. | | | 0 | 1 | FOLLOW MANU. INSTALLATION INSTRUCTIONS. |
| 2 | EXTERIOR | HALL | 3'-0" | 7'-0" | B | INSUL. METAL | PAINTED | H.M. | PAINTED | A570 | 0 | 2 | FOLLOW MANU. INSTALLATION INSTRUCTIONS. |
| 3 | EXEC. OFFICE | HALL | 3'-0" | 7'-0" | C | WOOD | STAINED | H.M. | PAINTED | A570 | 0 | 8 | FOLLOW MANU. INSTALLATION INSTRUCTIONS. |
| 4 | HALL | CONFERENCE ROOM | 3'-0" | 7'-0" | C | WOOD | STAINED | H.M. | | | 0 | 8 | FOLLOW MANU. INSTALLATION INSTRUCTIONS. |
| 5 | HALL | WOMENS | 3'-0" | 7'-0" | C | WOOD | STAINED | H.M. | | | 0 | 11 | FOLLOW MANU. INSTALLATION INSTRUCTIONS. |
| 6 | MENS | HALL | 3'-0" | 7'-0" | C | WOOD | STAINED | H.M. | | | 0 | 11 | FOLLOW MANU. INSTALLATION INSTRUCTIONS. |
| 7 | STORAGE | HALL | 3'-0" | 7'-0" | C | WOOD | STAINED | H.M. | | | 0 | 9 | FOLLOW MANU. INSTALLATION INSTRUCTIONS. |
| 8 | HALL | BREAK ROOM | 3'-0" | 7'-0" | C | WOOD | STAINED | H.M. | PAINTED | A570 | 0 | 9 | 6" W x 30" H VISION PANEL |
| 9 | OFFICE 1 | HALL | 3'-0" | 7'-0" | C | WOOD | STAINED | H.M. | PAINTED | A570 | 0 | 8 | FOLLOW MANU. INSTALLATION INSTRUCTIONS. |
| 10 | OFFICE 2 | HALL | 3'-0" | 7'-0" | C | WOOD | STAINED | H.M. | PAINTED | A570 | 0 | 8 | FOLLOW MANU. INSTALLATION INSTRUCTIONS. |
| 11 | OFFICE 3 | HALL | 3'-0" | 7'-0" | C | WOOD | STAINED | H.M. | PAINTED | A570 | 0 | 8 | FOLLOW MANU. INSTALLATION INSTRUCTIONS. |
| 12 | OFFICE 1 | WAREHOUSE | 3'-0" | 7'-0" | B | INSUL. METAL | PAINTED | H.M. | | | 3 | 7 | FOLLOW MANU. INSTALLATION INSTRUCTIONS. |
| 13 | HALL | WAREHOUSE | 3'-0" | 7'-0" | B | INSUL. METAL | PAINTED | H.M. | | | 3 | 7 | FOLLOW MANU. INSTALLATION INSTRUCTIONS. |
| 14 | WAREHOUSE | BREAK ROOM | 3'-0" | 7'-0" | B | INSUL. METAL | PAINTED | H.M. | PAINTED | A570 | 3 | 7 | 6" W x 30" H VISION PANEL |
| 15 | UNISEX | WAREHOUSE | 3'-0" | 7'-0" | B | INSUL. METAL | | H.M. | | | 0 | 10 | FOLLOW MANU. INSTALLATION INSTRUCTIONS. |
| 16 | EXTERIOR | WAREHOUSE | 3'-0" | 7'-0" | B | INSUL. METAL | PAINTED | H.M. | PAINTED | A570 | 0 | 02 | FOLLOW MANU. INSTALLATION INSTRUCTIONS. |
| 17 | EXTERIOR | WAREHOUSE | 3'-0" | 7'-0" | B | INSUL. METAL | PAINTED | H.M. | PAINTED | A570 | 0 | 02 | FOLLOW MANU. INSTALLATION INSTRUCTIONS. |
| 18 | WAREHOUSE | EXTERIOR | 10'-0" | 14'-0" | D | INSUL. O.H. | COILING | FACTORYPTD | - | A570 | 0 | 13 | LEFT HAND ELEC. OPERATOR w/ CONSTANT CONTACT OPERATION |
| 19 | EXTERIOR | WAREHOUSE | 10'-0" | 14'-0" | D | INSUL. O.H. | COILING | FACTORYPTD | - | A570 | 0 | 13 | RIGHT HAND ELEC. OPERATOR w/ CONSTANT CONTACT OPERATION |
| 20 | EXTERIOR | WAREHOUSE | 10'-0" | 14'-0" | D | INSUL. O.H. | COILING | FACTORYPTD | - | A570 | 0 | 13 | RIGHT HAND ELEC. OPERATOR w/ CONSTANT CONTACT OPERATION |
| 21 | EXTERIOR | WAREHOUSE | 10'-0" | 14'-0" | D | INSUL. O.H. | COILING | FACTORYPTD | - | A570 | 0 | 13 | RIGHT HAND ELEC. OPERATOR w/ CONSTANT CONTACT OPERATION |
| 22 | EXTERIOR | WAREHOUSE | 3'-0" | 7'-0" | B | INSUL. METAL | PAINTED | H.M. | PAINTED | A570 | 0 | 02 | FOLLOW MANU. INSTALLATION INSTRUCTIONS. |

Note: source renders widths as `3' - 0"` with spaces around the hyphen.
Decide once whether to normalize, apply it everywhere, and record the choice.

Twenty minutes of transcription converts "looks right" into a test that fails
loudly when a refactor breaks row stitching. **Every extraction project that
skips this ends up unable to distinguish improvement from regression.**

### 8.2 Get more PDFs

Every threshold here is fitted to **one document**. During Phase 1, collect
5–10 more bid sets and run the finder across all of them. That is what turns
"tuned on one sample" into "actually works". Track for each: pages scanned,
pages found, whether the found page was correct, which `method` fired.

---

## 9. Reference notes on the existing TypeScript app

For **reading only** — mine these for hard-won lessons, don't import anything.
Paths relative to `c:\planckoff\planckoff-hardware\`.

| File | What's worth stealing |
|---|---|
| `services/hardwarePdfServiceV2.ts` | Tier orchestration: try in order, record which won, a failed tier never kills the request |
| `services/hardwarePdf/textExtraction.ts` (`isTextReadable`, ~line 71) | Gates that reject *present but unusable* text before spending tokens. The comments explain real documents that broke the pipeline |
| `services/hardwarePdf/responseParser.ts` | Truncation recovery — walking partial JSON to salvage complete objects after a token-limit cut-off |
| `lib/ai/doorScheduleGrid.ts` | Deterministic table recovery from text positions. Note how much work goes into pixel-sampling that PyMuPDF's `get_drawings()` makes trivial |
| `lib/db/hardware.ts` (`DoorScheduleRow`) | The canonical field names the frontend already expects — align to these where sensible |

> The doc at `docs/architecture/03_AI_WORKFLOW_AND_PERFORMANCE_HARDWARE.md`
> describes an **older** design (client-side extraction, direct Gemini, different
> limits) that no longer matches the running code. Trust the source, not that doc.

---

## 10. Definition of done

- [ ] `POST /extract` with the 46 MB Ellis County set returns 23 rows in < 5 s
- [ ] Response reports `"method": "deterministic"` and `"source_pages": [21]`
- [ ] **Zero AI tokens** consumed on that document
- [ ] `pytest` green, including the golden-file test
- [ ] Page finder validated against ≥ 5 bid sets, results recorded
- [ ] `/docs` renders a complete, accurate OpenAPI schema
- [ ] Every failure mode in §7 returns its intended message, not a 500
- [ ] `README.md` documents setup, run, and the `.env` keys

---

## 11. Ordered first steps

1. `git init`; create the layout in §4
2. `python -m venv .venv` (Python 3.11); install PyMuPDF, FastAPI, uvicorn, pydantic, pydantic-settings, pdfplumber, openai, pytest
3. Extract `tests/fixtures/ellis_p21.pdf` using the snippet in §4
4. **Phase 0** — skeleton, verify `page_count: 102` on the real file
5. **Phase 1** — port the page finder; verify it returns exactly `[21]` in < 5 s
6. Only then continue to Phase 2

Do not start Phase 4 (AI) until Phases 1–3 pass on the fixture. The whole point
of the architecture is that AI is the exception, not the path.
