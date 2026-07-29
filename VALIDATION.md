# Validation record

Every threshold in the page finder was fitted to one document. This file records
what has actually been measured, so "tuned on one sample" can be told apart from
"works". Measured 2026-07-29 on Windows 11, Python 3.11.9, PyMuPDF 1.28.0.

Reproduce with:

```bash
python scripts/validate_corpus.py corpus --json corpus_results.json
```

The harness runs with `allow_ai=False`, so it can never spend tokens.

## Corpus — 4 documents

| Document | Pages | Size | Schedule page | Found | Correct | Method | Rows | Scan |
|---|---|---|---|---|---|---|---|---|
| `1780995376_ELLIS_COUNTY_Bid Set.pdf` | 102 | 44.1 MB | 21 (A560) | 21 | ✅ | `deterministic_ruled` | 23 | 1.5 s |
| `GRACEM_2.PDF` | 70 | 60.3 MB | 41 | 41 | ✅ | `deterministic_ruled` | 39 | 7–13 s |
| `Weston_-_Architectural_Spec…pdf` | 393 | 8.5 MB | *(none — spec text)* | none | ✅ | — | — | 3.0 s |
| `ASSEMBLIES.pdf` | 9 | 6.3 MB | *(none — floor/ceiling)* | none | ✅ | — | — | 0.7 s |

**4 of 4 correct: 2 true positives, 2 true negatives. Zero AI tokens, zero false
positives.**

The two negatives matter as much as the positives. A pipeline that finds a door
schedule in a document that has none is worse than one that finds nothing —
especially with a vision tier downstream, which will invent plausible rows from
whatever page it is handed.

- **Weston** is a 393-page written specification, not drawings. Its best page
  (220) scores `header_hits=1, tag_run=37` — the long tag run is clause
  numbering, and the header gate correctly rejects it.
- **ASSEMBLIES** is floor and ceiling assemblies. Its page 4 contains **zero**
  occurrences of "DOOR", "FRAME", "HARDWARE" or "THRESHOLD", and scores
  `header_hits=1, tag_run=9`. The tag run comes from assembly codes (`FL10`,
  `FL11`, …), which are shaped exactly like door tags. Only the header gate
  separates it.

## Page-finder separation

The gates are `header_hits >= 5` and `tag_run >= 8`. What matters is not that
the winner passes, but the margin between it and everything else.

| Document | Winner | Runner-up | Verdict |
|---|---|---|---|
| Ellis | p21 `hits=12 run=22` | p55 `hits=1 run=24` | passes on headers, huge margin |
| GRACEM | p41 `hits=9 run=39` | p40 `hits=2 run=29` | passes, margin 9 vs 2 |
| Weston | p220 `hits=1 run=37` | p221 `hits=1 run=35` | correctly rejected |
| ASSEMBLIES | p4 `hits=1 run=9` | p3 `hits=2 run=6` | correctly rejected |

`tag_run` alone separates nothing — the two negatives post runs of 37 and 9,
higher and comparable to real schedules. **`header_hits` is doing all the
discriminating work**: 12 and 9 for real schedules, 1 and 1 for the negatives.
The gate of 5 currently sits in a wide empty gap, which is the healthiest signal
in this table.

On Ellis, keyword matching alone returned 17 of 102 pages, including page 19,
which contains the literal string "DOOR SCHEDULE" in a sheet index and no table.
It scores `header_hits=1, tag_run=2` and is rejected.

## Extraction accuracy

**Ellis p21** — all 23 rows verified field-for-field against the ground truth in
PLAN.md §8.1 and frozen as `tests/fixtures/ellis_p21_expected.json`. Covers both
hard cases: the unnumbered opening row is preserved rather than swallowed as a
continuation, and wrapped cells are joined (`CONFERENCE ROOM`,
`FOLLOW MANU. INSTALLATION INSTRUCTIONS.`).

**GRACEM p41** — 39 rows, different firm, different layout: no `FROM`/`TO`, and
the frame split into head/jamb/sill details. Verified that zero text items
inside the table bounds were left unassigned to a column. The five columns with
no canonical equivalent (`DOOR GLAZING`, `FRAME TYPE`, `FRAME HEAD DETAIL`,
`FRAME JAMB DETAIL`, `FRAME SILL DETAIL`) are preserved in each row's `extra`.

Its `HARDWARE` column is empty for all 39 rows. Confirmed against the raw cell
grid: genuinely blank on that sheet, not a mapping failure.

## Bug found by this corpus

ASSEMBLIES triggered a real defect, now fixed and covered by
`test_small_doc_guess_is_never_reported_as_a_find`.

For documents of ≤ 20 pages the pipeline nominates the best-scoring page for the
AI tier even when nothing passed the gates. That nomination is a guess — but the
failure path reported it using the "page found" message, so a 9-page document
with no door schedule anywhere returned:

> Found a door schedule on page 4 but could not read any rows.

It now correctly returns `No door schedule found — scanned 9 pages.` The guess
is still made (it is a reasonable one on a small document), but it is no longer
reported as a find.

## Timing

| Stage | Ellis (102 pp, 44 MB) |
|---|---|
| Full-document structural scan | 1.5 s |
| Table location + extraction (1 page) | 0.02 s |
| **End-to-end HTTP, including upload** | **2.3 s** |

Disabling image decoding in `get_text()` took the Ellis scan from 7.9 s to
1.4 s — on a bid set full of rasters, decoding embedded images dominated a
text-only pass.

**GRACEM scans in 7–13 s, roughly 10× slower per page than Ellis** (~0.15 s/page
vs ~0.014 s/page). Investigated rather than assumed:

- Not a single bad page — cost is spread evenly, slowest page 0.37 s.
- Not the corrupt xref — `PdfDoc` open is 0.01 s. (PyMuPDF logs recoverable
  `cannot find object in xref` errors on this file; it repairs and continues.)
- Not a suboptimal extraction mode. Measured on this document:
  `dict` **7.3 s**, `words` 12.9 s, `rawdict` 13.4 s. The current choice is
  already the fastest.

The 7 s floor is inherent to MuPDF on these pages; the swing to 13 s tracks
machine load, not input. Cutting it further means parallelising the scan across
pages, which is deliberately out of scope for the POC.

## Known gaps

1. **Four documents, two of them positive.** Better than one, still short of a
   confident sample for retuning thresholds. `POST /api/v1/door-schedule/inspect`
   scores a new bid set without running extraction; add results to the table above.
2. **GRACEM's scan exceeds the 5 s target.** Characterised above, not fixed.
3. **GRACEM is 60.3 MB, over the 50 MB cap**, so over HTTP it returns a 413 and
   was tested through the library. Either raise `MAX_UPLOAD_MB` or treat 50 MB as
   a real product constraint.
4. **The AI fallback has never fired on real input.** Its parser is unit-tested,
   but no document in hand has needed it. Its true trigger rate is unknown until
   a scanned or image-only sheet is tested — and no document in this corpus is
   scanned.
5. **Multi-page schedules are not handled.** The pipeline takes the single best
   page rather than concatenating. No document in hand does this.
6. **Width normalization.** Values are returned exactly as printed
   (`3' - 0"`, spaces around the hyphen). No normalization anywhere; if the
   frontend wants `3'-0"` that is a decision still to be made.
