"""Pricing a maker's items with AI, end to end: pages, prices, checks.

    step A   one call names the pages for every distinct product (page_finder)
    step B   both models read those pages, side by side (price_reader)
    checks   each answer's prices must be printed on its pages, and the two
             models must agree (ai_check)

What comes out is a verdict per product, never a bare price. Storing it and
showing it for a person to confirm is the caller's.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field

from app.price_book import ai_check, page_finder, price_reader
from app.price_book.makers import Written


@dataclass
class Spent:
    step: str            # A | B
    model: str
    calls: int
    pages: int
    prompt_tokens: int
    completion_tokens: int
    usd: float


@dataclass
class MakerPriced:
    manufacturer: str
    found: list[page_finder.Found]
    judged: list[ai_check.Judged]
    spent: list[Spent] = field(default_factory=list)

    @property
    def usd(self) -> float:
        return sum(s.usd for s in self.spent)


async def price_maker(maker: str, books: list[page_finder.Book], items: list[Written],
                      model: str, check_model: str = "") -> MakerPriced:
    a = await page_finder.find_pages(maker, books, items, model)
    models = [model] + ([check_model] if check_model and check_model != model else [])
    reads = await asyncio.gather(*(price_reader.read_prices(maker, a.found, m) for m in models))
    judged = await asyncio.to_thread(ai_check.judge_all, {r.model: r.reads for r in reads})
    out = MakerPriced(maker, a.found, judged)
    out.spent.append(Spent("A", a.model, a.calls, 0, a.prompt_tokens, a.completion_tokens, a.usd))
    for r in reads:
        out.spent.append(Spent("B", r.model, r.calls, r.pages_sent, r.prompt_tokens,
                               r.completion_tokens, r.usd))
    return out


# --------------------------------------------------------------------------- #
# a project's items: every maker, stored, verified prices reused
# --------------------------------------------------------------------------- #

def _answer(c: ai_check.Checked) -> dict:
    r = c.read
    return {"model": c.model, "status": r.status, "total": r.total if c.priced else None,
            "printed": c.printed, "problems": c.problems, "note": r.note, "options": r.options,
            "parts": [{**asdict(p), "extended": p.extended} for p in r.parts]}


def _row(maker: str, product: page_finder.Product, found: page_finder.Found,
         judged: ai_check.Judged, run_id: str | None) -> dict:
    agreed = next((a for a in judged.answers if a.priced), None) if judged.verdict == "agreed" else None
    return {
        "manufacturer": maker, "product_key": product.product_key,
        "match_key": product.product_key.split("|")[0], "model": product.model,
        "finish": product.finish, "size": product.size, "name": product.name,
        "description": product.description, "verdict": judged.verdict,
        "list_price": judged.total if agreed else None,
        "parts": _answer(agreed)["parts"] if agreed else [],
        "answers": [_answer(a) for a in judged.answers],
        "pages": [asdict(p) for p in found.pages], "run_id": run_id,
        # Cleared, so a product the agents priced before keeps none of their words.
        "engine": "pipeline", "notes": [], "needs": [], "orderable": True, "reasons": [],
    }


async def price_project(items: list[Written], run_id: str | None, model: str, check_model: str,
                        reuse_verified: bool = True) -> dict:
    """Price every item with AI, maker by maker, and store each product's verdict.

    A product a person has already verified is not read again: it comes back
    from the table as `verified`. Items whose maker has no loaded or mapped
    book come back as `not_looked_up`, with the reason.
    """
    from app.price_book import makers, store

    groups = makers.group(items)

    def _resolve():
        books = store.mapped_books()
        codes = store.maker_codes()
        out = {}
        for key, g in groups.items():
            maker, _, _ = makers.resolve(g.written, set(books), codes)
            out[key] = maker
        return out, books

    resolved, books = await asyncio.to_thread(_resolve)
    per_maker: dict[str, list[Written]] = {}
    answers: dict[str | None, dict] = {}
    for key, maker in resolved.items():
        for i in groups[key].items:
            if not maker or maker not in books:
                answers[i.id] = {"status": "not_looked_up",
                                 "reason": "No price book for this maker" if maker else "Maker not identified"}
            else:
                per_maker.setdefault(maker, []).append(i)

    spent: list[dict] = []
    for maker, its in per_maker.items():
        products = page_finder.products(its)
        known = await asyncio.to_thread(store.ai_prices_for, maker, [p.product_key for p in products]) \
            if reuse_verified else {}
        todo = [i for i in its if "|".join(i.product()) not in known or not known["|".join(i.product())]["verified"]]
        for p in products:
            row = known.get(p.product_key)
            if row and row["verified"]:
                for item_id in p.item_ids:
                    answers[item_id] = {"status": "verified", "manufacturer": maker, **_public(row)}
        if not todo:
            continue
        maps = await asyncio.to_thread(store.book_maps, books[maker])
        if not maps:
            for i in todo:
                answers[i.id] = {"status": "not_looked_up", "manufacturer": maker,
                                 "reason": "This maker's books have not been mapped yet"}
            continue
        priced = await price_maker(maker, maps, todo, model, check_model)
        spent.extend(asdict(s) | {"manufacturer": maker} for s in priced.spent)
        found_by_key = {f.product.key: f for f in priced.found}
        for j in priced.judged:
            f = found_by_key[j.key]
            row = await asyncio.to_thread(store.save_ai_price, _row(maker, f.product, f, j, run_id))
            for item_id in f.product.item_ids:
                answers[item_id] = {"status": j.verdict, "manufacturer": maker, **_public(row)}
        for f in priced.found:
            if f.product.key not in {j.key for j in priced.judged}:
                for item_id in f.product.item_ids:
                    answers[item_id] = {"status": "unsure", "manufacturer": maker,
                                        "reason": "No page was found for it"}

    counts: dict[str, int] = {}
    for a in answers.values():
        counts[a["status"]] = counts.get(a["status"], 0) + 1
    return {"items": [{"id": i.id, **answers.get(i.id, {"status": "unsure"})} for i in items],
            "counts": counts, "spent": spent, "usd": round(sum(s["usd"] for s in spent), 6)}


def _public(row: dict) -> dict:
    """What an item answer shows of a stored AI price."""
    return {"ai_price_id": row.get("id"),
            **{k: row.get(k) for k in ("list_price", "verdict", "verified", "parts", "answers",
                                       "pages", "verified_note")}}


# --------------------------------------------------------------------------- #
# stored AI prices in the shapes the frontend already reads
# --------------------------------------------------------------------------- #

def _readings(row: dict, model: str, item: Written) -> list[dict]:
    """What each AI model read for a price that was not settled, as choices.

    Shown so a person starts from the book's page and the numbers read there
    rather than from nothing; never counted as a price until one is chosen.
    A reading the checks held back is still shown, with why it was held.
    """
    out: dict[float, dict] = {}
    for a in row.get("answers") or []:
        parts = a.get("parts") or []
        total = a.get("total")
        if total is None:
            total = sum(p.get("extended", p.get("amount")) or 0 for p in parts)
        total = round(float(total), 2)
        if total <= 0:
            continue
        held = (a.get("problems") or [a.get("note") or ""])[0] if a.get("total") is None else ""
        o = out.setdefault(total, {"model": model, "size": item.size, "finish": item.finish,
                                   "list_price": total, "description": a.get("label") or "read by AI",
                                   "book": parts[0].get("book", "") if parts else "",
                                   "pdf_page": parts[0].get("pdf_page") if parts else None,
                                   "row": parts[0].get("row", "") if parts else "",
                                   "read_by": [], "held_because": held})
        o["read_by"].append(a.get("model", ""))
    return sorted(out.values(), key=lambda o: o["list_price"])


def _why_unsettled(row: dict, item: Written) -> list[str]:
    """Each model's own words on why it gave no price, so the row says why
    without the run it came from."""
    if not (item.model or item.description).strip():
        return ["The schedule gives no model number to look up"]
    if row.get("engine") == "agent":
        # The agents say why in their own words: the checker's, and what to specify.
        return [str(r) for r in (row.get("reasons") or []) + (row.get("needs") or [])][:6]
    out: list[str] = []
    for a in row.get("answers") or []:
        if a.get("status") == "not_on_pages":
            why = "No page in the book was found for this product"
        else:
            why = (a.get("note") or (a.get("problems") or [""])[0] or "").strip()
        name = (a.get("model") or "").split("/")[-1]
        if why and not any(why in o for o in out):
            out.append(f"{name}: {why}" if name else why)
    return out[:4]


def parts_for(row: dict, price: float) -> list[dict] | None:
    """The parts of the answer a confirmed price is, so a reused price keeps its pages.

    A person confirming one of an `options` product's prices (rose $1,841 or
    escutcheon $1,870) sends that option's total; its parts are kept with it.
    None when the price is none of the answers' totals -- typed from the page --
    and the row's parts are left as they are.
    """
    if row.get("verdict") == "agreed" and row.get("parts"):
        return None
    for a in row.get("answers") or []:
        if a.get("role") != "checker" and a.get("total") is not None and a.get("parts") \
                and abs(float(a["total"]) - price) < 0.005:
            return a["parts"]
    return None


def as_quote(maker: str, item: Written, quantity: float, row: dict | None) -> dict:
    """An item's stored AI price in the shape POST /prices has always answered.

    priced        a person confirmed it
    needs_review  two models agreed on it, nobody has confirmed it yet: the
                  price is given, with that said; or the AI could not settle
                  it and a person has to (no price)
    not_found     the product has not been priced with AI yet
    """
    # ai_price_id is what POST /price-books/ai-prices/{id}/confirm takes; null
    # when the product has no stored AI price to confirm.
    base = {"ai_price_id": row.get("id") if row else None,
            "manufacturer": maker, "model": item.model or item.description, "finish": item.finish,
            "size": item.size, "list_price": None, "extended_price": None, "parts": [],
            "reasons": [], "options": [], "book": "", "effective_date": None}
    if row is None:
        return {**base, "status": "not_found",
                "reasons": ["Not priced yet: run AI pricing for this project (POST /price-books/ai-price)"]}
    if row.get("list_price") is None:
        first = ("The code leaves a choice open; each option below was checked, a person picks one"
                 if row.get("verdict") == "options" else
                 "The AI could not settle this price from the book; a person confirms it")
        return {**base, "status": "needs_review",
                "reasons": [first] + _why_unsettled(row, item),
                "options": _readings(row, base["model"], item)}
    price = float(row["list_price"])
    parts = [{"label": p.get("label", ""), "list_price": p.get("extended", p.get("amount")),
              "pdf_page": p.get("pdf_page"), "printed_page": "", "book": p.get("book", "")}
             for p in row.get("parts") or []]
    out = {**base, "list_price": price, "extended_price": round(price * quantity, 2), "parts": parts,
           "book": parts[0]["book"] if parts else ""}
    if row.get("verified"):
        return {**out, "status": "priced"}
    return {**out, "status": "needs_review",
            "reasons": ["Read by AI and agreed by two models; not yet confirmed by a person"]}


def as_suggestion(row: dict) -> dict:
    """A stored AI price in the shape GET /price-books/search has always answered."""
    part = (row.get("parts") or [{}])[0]
    page = next((p for p in row.get("pages") or [] if p.get("pdf_page") == part.get("pdf_page")), {})
    return {"ai_price_id": row.get("id"),
            "model": row.get("model") or row.get("description", ""), "finish": row.get("finish", ""),
            "size": row.get("size", ""), "description": row.get("name") or row.get("description", ""),
            "list_price": float(row["list_price"]), "book": part.get("book", ""),
            "pdf_page": part.get("pdf_page"), "printed_page": page.get("printed_page", ""),
            "effective_date": None, "verified": bool(row.get("verified"))}
