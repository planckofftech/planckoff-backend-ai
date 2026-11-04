"""Pricing a project's items with the agents: the orchestrator and the verdict.

The second engine beside ai_pricing (step A and step B). Per distinct product:

    Agent 2   prices it from the maker's books, starting from each book's guide
              where one is made (pricing_agent)
    checks    every amount is printed on its page, rates are caught, code adds
              each option's parts (ai_check.check_printed, as for step B)
    Agent 3   a different model checks the reading of the code and each part
              on the pages used (checker_agent)
    verdict   from the three:

      agreed      one option; code checks pass; the checker agrees with the
                  reading and every part. Has a price, still unconfirmed
      options     the same, but the code leaves a choice open (rose or
                  escutcheon): every option has a checked price, a person picks
      disagree    the checker disagrees with the reading or a part
      one_answer  priced and checked by code, but the checker gave no answer
      unsure      no full price, a price not printed on its page, or the book
                  says the code cannot be ordered as written

A product a person verified is reused, as in ai_pricing. Products run a few at
a time, and each item's progress -- queued, the step its agent is on, its
answer -- is written as it moves, so the frontend shows rows as they finish.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from app.price_book import agent, ai_check, book_skills, checker_agent, guide_agent, page_finder, pricing_agent
from app.price_book.ai_pricing import Spent, _public
from app.price_book.makers import Written
from app.price_book.price_reader import Read

log = logging.getLogger(__name__)

# Products priced at once in one run.
PARALLEL = 4


@dataclass
class Option:
    label: str
    read: Read
    printed: bool                        # code checks passed
    problems: list[str]
    checker_agrees: bool

    @property
    def total(self) -> float | None:
        return self.read.total if self.printed and self.read.status == "priced" else None


@dataclass
class Judged:
    verdict: str                         # agreed | options | disagree | one_answer | unsure
    total: float | None                  # only when agreed
    options: list[Option] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def judge(priced: pricing_agent.Priced, checked: checker_agent.Checked,
          pages: ai_check.PageText) -> Judged:
    """The verdict on one product from Agent 2's answer, the code checks and Agent 3."""
    options = []
    for i, (label, read) in enumerate(priced.options, 1):
        c = ai_check.check_printed(priced.outcome.model if priced.outcome else "", read, pages)
        options.append(Option(label, read, c.priced, c.problems,
                              checked.done and checked.option_agrees(i, len(read.parts))))
    if priced.status not in ("priced", "options") or not options:
        return Judged("unsure", None, options, priced.notes[:4] or ["No price was found in the book"])
    held = [p for o in options for p in o.problems if not o.printed]
    if held or not all(o.printed for o in options):
        return Judged("unsure", None, options, held[:4] or ["A part could not be checked on its page"])
    if any(o.total is not None and o.total <= 0 for o in options):
        # Deductions are allowed as parts; a whole price of nothing or less is not.
        return Judged("unsure", None, options, ["The parts add up to no price"])
    if not priced.orderable:
        return Judged("unsure", None, options,
                      ["The book says this code cannot be ordered as written"] + priced.needs_from_person[:3])
    if not checked.done:
        return Judged("one_answer", None, options, [checked.note or "The checker gave no answer"])
    if not checked.reading_agrees or not all(o.checker_agrees for o in options):
        why = [] if checked.reading_agrees else [f"Reading of the code: {checked.reading_why}"]
        why += [f"Option {o} part {p}: {c.why}" for (o, p), c in sorted(checked.parts.items()) if not c.agree]
        return Judged("disagree", None, options, why[:4])
    if len(options) == 1:
        return Judged("agreed", options[0].total, options)
    return Judged("options", None, options, priced.needs_from_person[:3])


def _row(maker: str, product: page_finder.Product, priced: pricing_agent.Priced,
         checked: checker_agent.Checked, judged: Judged, run_id: str | None) -> dict[str, Any]:
    """The ai_prices row: each option is one answer, the checker's word another."""
    model = priced.outcome.model if priced.outcome else ""
    answers = [{"model": model, "label": o.label, "status": o.read.status, "total": o.total,
                "printed": o.printed, "problems": o.problems, "note": o.label,
                "checker_agrees": o.checker_agrees,
                "parts": [{**asdict(p), "extended": p.extended} for p in o.read.parts]}
               for o in judged.options]
    answers.append({"model": checked.model, "role": "checker", "status": "checked" if checked.done else "none",
                    "total": None, "parts": [], "reading_agrees": checked.reading_agrees,
                    "note": checked.reading_why if checked.done and not checked.reading_agrees
                    else checked.note, "problems": [c.why for c in checked.parts.values() if not c.agree],
                    "missed": checked.missed})
    agreed = judged.options[0] if judged.verdict == "agreed" else None
    opened = priced.outcome.pages if priced.outcome else []
    return {
        "manufacturer": maker, "product_key": product.product_key,
        "match_key": product.product_key.split("|")[0], "model": product.model,
        "finish": product.finish, "size": product.size, "name": product.name,
        "description": product.description, "verdict": judged.verdict,
        "list_price": judged.total if agreed else None,
        "parts": answers[0]["parts"] if agreed else [], "answers": answers,
        "pages": [{"pdf_page": p} for p in opened], "run_id": run_id,
        "engine": "agent", "notes": priced.notes, "needs": priced.needs_from_person,
        "orderable": priced.orderable, "reasons": judged.reasons,
    }


def _spent(outcomes: list[agent.Outcome]) -> list[Spent]:
    by: dict[tuple[str, str], Spent] = {}
    for o in outcomes:
        s = by.setdefault((o.step, o.model), Spent(o.step, o.model, 0, 0, 0, 0, 0.0))
        s.calls += o.calls
        s.pages += len(o.pages)
        s.prompt_tokens += o.prompt_tokens
        s.completion_tokens += o.completion_tokens
        s.usd += o.billed_usd or o.usd
    return list(by.values())


def _step(turn: agent.Turn) -> str:
    """A turn as the frontend shows it while an item is being priced."""
    a = turn.args
    where = f" p.{a['page']}" if "page" in a else ""
    if turn.skill == "search_book":
        return f"Searching for {a.get('text', '')}"
    if turn.skill in ("read_page", "page_image"):
        return f"Reading{where}"
    if turn.skill == "book_contents":
        return "Reading the contents"
    if turn.skill == "submit_price":
        return "Checking the price"
    if turn.skill == "submit_check":
        return "Checked"
    return turn.skill


async def price_project(items: list[Written], run_id: str | None, model: str, check_model: str,
                        reuse_verified: bool = True) -> dict:
    """Price every item with the agents and store each product's verdict."""
    from app.price_book import makers, store

    groups = makers.group(items)

    def _resolve():
        books = store.mapped_books()
        codes = store.maker_codes()
        return {key: makers.resolve(g.written, set(books), codes)[0] for key, g in groups.items()}, books

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

    async def progress(item_ids: list[str | None], status: str, step: str = "",
                       answer: dict | None = None) -> None:
        if run_id:
            await asyncio.to_thread(store.item_progress, run_id, item_ids, status, step, answer)

    for item_id, a in answers.items():
        await progress([item_id], "done", answer=a)

    outcomes: list[agent.Outcome] = []
    gate = asyncio.Semaphore(PARALLEL)
    if check_model == model:
        # The checker must be another model to be worth anything (see ai_check).
        log.warning("agent pricing: the check model is the pricing model; nothing will be agreed")

    async def one(maker: str, product: page_finder.Product, shelf: book_skills.Shelf, guide: str) -> None:
        async with gate:
            async def on_turn(turn: agent.Turn) -> None:
                await progress(product.item_ids, "working", _step(turn))

            await progress(product.item_ids, "working", "Starting")
            pages = ai_check.PageText()
            try:
                priced = await pricing_agent.price(product, maker, shelf, model, guide, on_turn)
                if priced.outcome:
                    outcomes.append(priced.outcome)
                checked = checker_agent.Checked(check_model, False, note="No second model is set") \
                    if not check_model or check_model == model else \
                    await checker_agent.check(priced, maker, shelf, check_model, guide, on_turn)
                if checked.outcome:
                    outcomes.append(checked.outcome)
                judged = await asyncio.to_thread(judge, priced, checked, pages)
                row = await asyncio.to_thread(store.save_ai_price,
                                              _row(maker, product, priced, checked, judged, run_id))
                answer = {"status": judged.verdict, "manufacturer": maker, **_public(row),
                          "reasons": judged.reasons, "notes": priced.notes,
                          "needs": priced.needs_from_person, "orderable": priced.orderable}
            except Exception as exc:  # noqa: BLE001 - one product's failure is its own
                from app.ai.client import AiUnavailableError, AiUpstreamError
                if isinstance(exc, (AiUnavailableError, AiUpstreamError)):
                    raise
                log.exception("agent pricing: %s %s failed", maker, product.product_key)
                answer = {"status": "unsure", "manufacturer": maker,
                          "reason": f"Pricing this product failed: {type(exc).__name__}"}
            finally:
                pages.close()
            for item_id in product.item_ids:
                answers[item_id] = answer
            await progress(product.item_ids, "done", answer=answer)

    jobs = []
    for maker, its in per_maker.items():
        products = page_finder.products(its)
        known = await asyncio.to_thread(store.ai_prices_for, maker, [p.product_key for p in products]) \
            if reuse_verified else {}
        todo = []
        for p in products:
            row = known.get(p.product_key)
            if row and row["verified"]:
                answer = {"status": "verified", "manufacturer": maker, **_public(row)}
                for item_id in p.item_ids:
                    answers[item_id] = answer
                await progress(p.item_ids, "done", answer=answer)
            else:
                todo.append(p)
        if not todo:
            continue
        maps = await asyncio.to_thread(store.book_maps, books[maker])
        shelf = book_skills.Shelf.of(books[maker], maps)
        stored = await asyncio.to_thread(store.guides, books[maker])
        guide = "\n\n".join(f"Guide to {label} ({name}):\n{guide_agent.render(stored[name]['guide'])}"
                            for label, name in shelf.books.items() if name in stored)
        jobs.extend(one(maker, p, shelf, guide) for p in todo)
    await asyncio.gather(*jobs)

    counts: dict[str, int] = {}
    for a in answers.values():
        counts[a["status"]] = counts.get(a["status"], 0) + 1
    spent = [asdict(s) for s in _spent(outcomes)]
    return {"items": [{"id": i.id, **answers.get(i.id, {"status": "unsure"})} for i in items],
            "counts": counts, "spent": spent, "usd": round(sum(s["usd"] for s in spent), 6)}
