"""The figures in the AI pricing cost and accuracy report, from what was stored.

Inputs are rows as Supabase holds them -- ai_price_calls, ai_prices,
ai_price_runs -- so the report is reproducible from the database alone. Each
sheet is (header, rows). scripts/ai_price_report.py writes them to xlsx.

Two figures are shares, said here so nobody reads them as measured per item:

  cost per page     a step B call's cost over the pages it sent. Each page goes
                    as an image with its text, so pages are what a call costs
  cost per product  a call's cost over the products it was about. A product
                    read in a call with nine others carries a tenth of it

Accuracy is against what a person confirmed: a model's answer is right when its
total is the verified list price, wrong when it is another price, unsure when
it gave none. Unverified products have no truth yet and are left out of it.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

Sheet = tuple[list[str], list[list[Any]]]
_CENT = 0.005


def _money(x: float) -> float:
    return round(float(x), 6)


def per_call(calls: list[dict]) -> Sheet:
    head = ["When", "Run", "Project", "Step", "Model", "Maker", "Pages sent", "Products",
            "Tokens in", "Tokens out", "USD", "Seconds", "Outcome"]
    return head, [[c["created_at"], c["run_id"] or "", c["project_id"] or "", c["step"], c["model"],
                   c["manufacturer"], len(c["pages"]), len(c["product_keys"]), c["prompt_tokens"],
                   c["completion_tokens"], _money(c["usd"]), float(c["seconds"]), c["outcome"]]
                  for c in calls]


def per_page(calls: list[dict], kind_of: Callable[[str, int], str]) -> Sheet:
    """Cost per page sent, by model and page kind (table, picture, mixed)."""
    acc: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0, 0.0])
    for c in calls:
        if c["step"] != "B" or not c["pages"]:
            continue
        share = float(c["usd"]) / len(c["pages"])
        book = c["books"][0] if len(c["books"]) == 1 else ""
        for page in c["pages"]:
            kind = kind_of(book, page) if book else "unknown (several books in the call)"
            acc[(c["model"], kind)][0] += 1
            acc[(c["model"], kind)][1] += share
    head = ["Model", "Page kind", "Pages read", "USD", "USD per page"]
    return head, [[m, k, n, _money(usd), _money(usd / n)] for (m, k), (n, usd) in sorted(acc.items())]


def per_product(calls: list[dict], prices: list[dict]) -> Sheet:
    cost: dict[tuple[str, str], float] = defaultdict(float)
    for c in calls:
        if c["step"] == "identify" or not c["product_keys"]:
            continue
        share = float(c["usd"]) / len(c["product_keys"])
        for k in c["product_keys"]:
            cost[(c["manufacturer"], k)] += share
    info = {(p["manufacturer"], p["product_key"]): p for p in prices}
    head = ["Maker", "Product", "Item name", "Verdict", "Verified", "List price", "AI USD"]
    rows = []
    for (maker, key), usd in sorted(cost.items()):
        p = info.get((maker, key), {})
        rows.append([maker, key.replace("|", " "), p.get("name", ""), p.get("verdict", ""),
                     "yes" if p.get("verified") else "no", p.get("list_price"), _money(usd)])
    return head, rows


def per_maker(calls: list[dict], prices: list[dict]) -> Sheet:
    usd: dict[str, float] = defaultdict(float)
    n_calls: dict[str, int] = defaultdict(int)
    for c in calls:
        if c["manufacturer"]:
            usd[c["manufacturer"]] += float(c["usd"])
            n_calls[c["manufacturer"]] += 1
    verdicts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for p in prices:
        verdicts[p["manufacturer"]][p["verdict"]] += 1
        if p["verified"]:
            verdicts[p["manufacturer"]]["verified"] += 1
    head = ["Maker", "Calls", "USD", "Products", "Agreed", "Disagree", "One answer", "Unsure",
            "Verified", "USD per product"]
    rows = []
    for maker in sorted(set(usd) | set(verdicts)):
        v = verdicts[maker]
        products = sum(v[k] for k in ("agreed", "disagree", "one_answer", "unsure"))
        rows.append([maker, n_calls[maker], _money(usd[maker]), products, v["agreed"], v["disagree"],
                     v["one_answer"], v["unsure"], v["verified"],
                     _money(usd[maker] / products) if products else None])
    return head, rows


def per_project(runs: list[dict]) -> Sheet:
    head = ["Project", "Runs", "Items", "USD", "Verified", "Agreed", "Disagree", "One answer",
            "Unsure", "Not looked up", "USD per item"]
    acc: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in runs:
        a = acc[r.get("project_id") or "(no project)"]
        a["runs"] += 1
        a["items"] += len(r.get("items") or [])
        a["usd"] += float(r.get("usd") or 0)
        for k, n in (r.get("counts") or {}).items():
            a[k] += n
    rows = []
    for project, a in sorted(acc.items()):
        rows.append([project, int(a["runs"]), int(a["items"]), _money(a["usd"]), int(a["verified"]),
                     int(a["agreed"]), int(a["disagree"]), int(a["one_answer"]), int(a["unsure"]),
                     int(a["not_looked_up"]), _money(a["usd"] / a["items"]) if a["items"] else None])
    return head, rows


def per_model(calls: list[dict], prices: list[dict]) -> Sheet:
    """Cost of each model, and how often it was right against confirmed prices."""
    acc: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for c in calls:
        a = acc[c["model"]]
        a["calls"] += 1
        a["usd"] += float(c["usd"])
        a["in"] += c["prompt_tokens"]
        a["out"] += c["completion_tokens"]
        a["cut_off"] += c["outcome"] == "cut_off"
    for p in prices:
        if not p["verified"] or p["list_price"] is None:
            continue
        truth = float(p["list_price"])
        for ans in p["answers"] or []:
            a = acc[ans["model"]]
            if ans.get("total") is None:
                a["unsure"] += 1
            elif abs(float(ans["total"]) - truth) <= _CENT:
                a["right"] += 1
            else:
                a["wrong"] += 1
    head = ["Model", "Calls", "USD", "USD per call", "Tokens in", "Tokens out", "Cut off",
            "Right", "Wrong", "Unsure", "Right when it answered"]
    rows = []
    for model, a in sorted(acc.items()):
        answered = a["right"] + a["wrong"]
        rows.append([model, int(a["calls"]), _money(a["usd"]),
                     _money(a["usd"] / a["calls"]) if a["calls"] else None, int(a["in"]), int(a["out"]),
                     int(a["cut_off"]), int(a["right"]), int(a["wrong"]), int(a["unsure"]),
                     round(a["right"] / answered, 3) if answered else None])
    return head, rows


def build(calls: list[dict], prices: list[dict], runs: list[dict],
          kind_of: Callable[[str, int], str]) -> dict[str, Sheet]:
    return {
        "Per model": per_model(calls, prices),
        "Per project": per_project(runs),
        "Per maker": per_maker(calls, prices),
        "Per product": per_product(calls, prices),
        "Per page": per_page(calls, kind_of),
        "Per call": per_call(calls),
    }
