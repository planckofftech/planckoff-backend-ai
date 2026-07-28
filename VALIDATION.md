# Validation record

Every threshold in the page finder was fitted to one document. This file records
what has actually been measured, so "tuned on one sample" can be told apart from
"works". Measured 2026-07-28 on Windows 11, Python 3.11.9, PyMuPDF 1.28.0.

## Documents tested

| Document | Pages | Size | Schedule page | Found | Correct | Method | Rows | Scan |
|---|---|---|---|---|---|---|---|---|
| `1780995376_ELLIS_COUNTY_Bid Set.pdf` | 102 | 46.2 MB | 21 (A560) | 21 | ✅ | `deterministic_ruled` | 23 | 1.4 s |
| `GRACEM_2.PDF` | 70 | 60.3 MB | 41 | 41 | ✅ | `deterministic_ruled` | 39 | 11.4 s |

**2 of 2 documents: correct page, deterministic extraction, zero AI tokens.**

## Page-finder separation

The gates are `header_hits >= 5` and `tag_run >= 8`. What matters is not that
the winner passes, but the margin between it and the runner-up.

**Ellis County** — one passing page out of 102:

```
page  21   header_hits=12  tag_run=22  tag_x=1124.2  score=46   PASS
page  55   header_hits= 1  tag_run=24                score=26
page  58   header_hits= 1  tag_run=20                score=22
page  71   header_hits= 2  tag_run=18                score=22
```

Page 19 contains the literal string "DOOR SCHEDULE" (a sheet index) and scores
`header_hits=1, tag_run=2` — rejected, which is the whole point of the
structural tests. Keyword matching alone returned 17 of 102 pages.

**GRACEM** — one passing page out of 70:

```
page  41   header_hits= 9  tag_run=39  tag_x=180.2   score=48   PASS
```

Note `header_hits=9` against a gate of 5. The margin is real but thinner than
Ellis's 12, and this is the number to watch as more documents are added.

## Extraction accuracy

**Ellis County p21** — all 23 rows verified field-for-field against the ground
truth transcribed in PLAN.md §8.1, and frozen as
`tests/fixtures/ellis_p21_expected.json`. Includes both hard cases:

- the unnumbered opening (`RECEPTION | HALL | 8'-0" | 7'-0" | OPEN | … | OPENING`)
  is preserved as row 1, not swallowed as a continuation
- wrapped cells joined: `CONFERENCE ROOM`,
  `FOLLOW MANU. INSTALLATION INSTRUCTIONS.`

**GRACEM p41** — 39 rows, a different firm with a different column layout:
no `FROM`/`TO`, and the frame split into head/jamb/sill details. Verified that
zero text items inside the table bounds were left unassigned to a column.
The five columns with no canonical equivalent (`DOOR GLAZING`, `FRAME TYPE`,
`FRAME HEAD DETAIL`, `FRAME JAMB DETAIL`, `FRAME SILL DETAIL`) are preserved in
each row's `extra` object rather than dropped.

Its `HARDWARE` column is empty for all 39 rows. Confirmed against the raw cell
grid: the column is genuinely blank on that sheet, not a mapping failure.

## Timing

| Stage | Ellis (102 pp, 46 MB) |
|---|---|
| Full-document structural scan | 1.4 s |
| Table location + extraction (1 page) | 0.02 s |
| **End-to-end HTTP, including upload** | **2.4 s** |

Disabling image decoding in `get_text()` took the scan from 7.9 s to 1.4 s — on
a bid set full of rasters, decoding embedded images dominated a text-only pass.

GRACEM scans in 11.4 s, ~8× slower per page than Ellis. Its pages carry far more
vector content, and its xref table needs repair on open (PyMuPDF logs recoverable
`cannot find object in xref` errors). **This is the main open performance
question** — see below.

## Known gaps

1. **Two documents is not a validated sample.** PLAN.md §8.2 calls for 5–10.
   The `/api/v1/door-schedule/inspect` endpoint exists to score new bid sets
   without running extraction; results should be added to the table above.
2. **GRACEM's 11.4 s scan exceeds the 5 s target.** Ellis meets it comfortably.
   Not yet diagnosed beyond "denser pages, xref repair on open".
3. **GRACEM is 60.3 MB, over the 50 MB cap**, so it is rejected over HTTP with a
   413 and was tested through the library directly. Either raise
   `MAX_UPLOAD_MB` or treat 50 MB as a real product constraint.
4. **The AI fallback has never fired on real input.** Its parser is unit-tested,
   but no document in hand has needed it. Its true trigger rate is unknown until
   a scanned or image-only sheet is tested.
5. **Multi-page schedules are not handled.** If a schedule continues onto a
   second sheet, the pipeline takes the single best page rather than
   concatenating. Neither document in hand does this.
6. **Width normalization.** Values are returned exactly as printed
   (`3' - 0"`, with spaces around the hyphen). No normalization is applied
   anywhere; if the frontend wants `3'-0"` that is a deliberate decision still
   to be made.
