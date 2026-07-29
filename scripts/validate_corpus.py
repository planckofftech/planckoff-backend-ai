"""Run the full pipeline over a folder of PDFs and tabulate what happened.

PLAN.md section 8.2: every threshold is fitted to one document, so the only way
to tell "tuned on one sample" from "works" is to run a corpus and record, per
document, which page was found and which tier fired.

    python scripts/validate_corpus.py <file-or-dir> [...] [--json out.json]

Uses the library directly, so the 50 MB HTTP upload cap does not apply.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import page_finder  # noqa: E402
from app.core.pdf_doc import NotAPdfError, PdfDoc  # noqa: E402
from app.pipeline import NoRowsError, NoScheduleFoundError, extract  # noqa: E402


async def run_one(path: Path) -> dict:
    data = path.read_bytes()
    row: dict = {
        "file": path.name,
        "size_mb": round(len(data) / 1024 / 1024, 1),
        "pages": None,
        "scan_ms": None,
        "candidates": [],
        "top_score": None,
        "runner_up": None,
        "method": "-",
        "rows": 0,
        "headers": [],
        "warnings": [],
        "outcome": "",
    }

    # Finder pass on its own, so scan cost is separated from extraction cost.
    try:
        started = time.perf_counter()
        with PdfDoc(data) as doc:
            row["pages"] = doc.page_count
            scores = page_finder.find_schedule_pages(doc)
        row["scan_ms"] = int((time.perf_counter() - started) * 1000)
    except NotAPdfError as exc:
        row["outcome"] = f"NOT A PDF ({exc})"
        return row

    ranked = sorted(scores, key=lambda c: c.score, reverse=True)
    hits = page_finder.passing(scores)
    row["candidates"] = [c.page for c in hits]
    if ranked:
        row["top_score"] = f"p{ranked[0].page} hits={ranked[0].header_hits} run={ranked[0].tag_run}"
    # The margin between the winner and the next page is what says whether the
    # gates are separating cleanly or barely, so never show the winner twice.
    runners = [c for c in ranked[1:] if c.page != ranked[0].page] if ranked else []
    if runners:
        row["runner_up"] = (f"p{runners[0].page} hits={runners[0].header_hits} "
                            f"run={runners[0].tag_run}")

    # Deterministic only -- this harness must never spend tokens.
    try:
        result = await extract(data, allow_ai=False)
    except NoScheduleFoundError:
        row["outcome"] = "no schedule found (correct if this doc has none)"
        return row
    except NoRowsError as exc:
        row["outcome"] = f"page found, no rows: {exc}"
        return row
    except Exception as exc:  # noqa: BLE001 - a harness must report, not crash
        row["outcome"] = f"ERROR {type(exc).__name__}: {exc}"
        return row

    row["method"] = result.method.value
    row["rows"] = result.row_count
    row["headers"] = result.headers
    row["warnings"] = result.warnings
    row["outcome"] = "extracted"
    return row


def collect(targets: list[str]) -> list[Path]:
    out: list[Path] = []
    for target in targets:
        p = Path(target)
        if p.is_dir():
            out.extend(sorted(q for q in p.iterdir()
                              if q.suffix.lower() == ".pdf" and q.is_file()))
        elif p.is_file():
            out.append(p)
        else:
            print(f"skipping missing path: {target}")
    return out


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("targets", nargs="+")
    parser.add_argument("--json", dest="json_out")
    parser.add_argument("--show-rows", type=int, default=0,
                        help="print this many extracted rows per document")
    args = parser.parse_args()

    results = []
    for path in collect(args.targets):
        print(f"\n{'=' * 78}\n{path.name}\n{'=' * 78}")
        row = await run_one(path)
        results.append(row)
        print(f"  pages      : {row['pages']}   ({row['size_mb']} MB)")
        print(f"  scan       : {row['scan_ms']} ms")
        print(f"  top page   : {row['top_score']}")
        print(f"  runner-up  : {row['runner_up']}")
        print(f"  candidates : {row['candidates'] or 'none'}")
        print(f"  outcome    : {row['outcome']}")
        if row["method"] != "-":
            print(f"  method     : {row['method']}")
            print(f"  rows       : {row['rows']}")
            print(f"  headers    : {row['headers']}")
        for warning in row["warnings"]:
            print(f"  warning    : {warning}")

        if args.show_rows and row["rows"]:
            result = await extract(path.read_bytes(), allow_ai=False)
            for r in result.rows[:args.show_rows]:
                d = r.model_dump()
                extra = d.pop("extra")
                print("     " + " | ".join(d.values())
                      + (f"   EXTRA={extra}" if extra else ""))

    print(f"\n\n{'=' * 78}\nSUMMARY\n{'=' * 78}")
    print(f"{'document':<46} {'pp':>4} {'scan':>7} {'page':>6} {'rows':>5}  method")
    for r in results:
        page = ",".join(str(p) for p in r["candidates"]) or "-"
        print(f"{r['file'][:45]:<46} {str(r['pages'] or '-'):>4} "
              f"{str(r['scan_ms'] or '-') + 'ms':>7} {page:>6} "
              f"{r['rows']:>5}  {r['method']}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
