"""Agent 3: check what the pricing agent handed in, on the pages it used.

A different model from the pricing agent's, so a mistake has to be made twice
to get through. It is given the product, the book guide, and Agent 2's answer
-- every option, every part with its page and the row it was read from -- and
may open only the pages Agent 2 cited or read. It does not search the book: its
job is to check the work, not to do it again.

Two things are checked, because the code checks cannot see either:

  the reading   is every piece of the code accounted for, in the place the
                book's order of a code gives it? Agent 2 read the L of
                LV9495EUL as L-escutcheon trim; the book puts the cylinder
                straight after the function, so that L is "less conventional
                cylinder". Every price it gave was printed on its page.
  each part     is this the right row and column for this code, and is it a
                price for one, not a rate or a pack?

Whether each amount is printed on its page, and the sum, stay with code
(ai_check.check_printed): that is arithmetic, not judgement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.price_book import agent, book_skills
from app.price_book.agent import Reply, Skill
from app.price_book.pricing_agent import Priced

STEP = "agent:check"
MAX_STEPS = 12
MAX_IMAGES = 4

SYSTEM = (
    "You check a colleague's price for a door hardware product, read from a manufacturer's "
    "price book. You are given the product as the schedule writes it, the book's guide, "
    "and your colleague's answer: one or more options, each made of parts, each part a "
    "price with the page it is printed on and the row and column it was read from. You "
    "may open those pages (text or picture). Check, do not redo: you cannot search the "
    "book.\n\n"
    "Check:\n"
    "1. The reading of the code: go through the code piece by piece, in the order the "
    "book says an order code is written, and say whether every piece was understood "
    "rightly (what it is, given its place) and whether any piece that costs something "
    "was left out or any part added that the code does not ask for.\n"
    "2. Each part: open its page and say whether it is the right row and the right column "
    "for this product (function, design or trim, finish), and whether its unit is right: a "
    "price for one, or a rate per foot or per area counted by the size the schedule or the "
    "option names (an option may assume a size the schedule leaves out; that is a choice, "
    "not an error). A product the book sells only by the pack or box is priced by the "
    "pack: the pack price is right.\n"
    "3. The notes: anything the pages say that the answer misses (not available with, "
    "must also be specified).\n\n"
    "The reading is only what each piece of the code means and what it adds. A choice the "
    "code leaves open that the answer already prices as options is not a reading error, "
    "and neither is anything about the notes: put what the notes miss in missed.\n\n"
    "B4E on a plate is bevelled 4 edges, the standard plate: its price is in the plate's "
    "price unless the book prints a charge for it, so pricing nothing for it is right.\n\n"
    "Schedules write codes loosely: no periods, options in any order, defaults left out "
    "(the cylinder or strike a base price already includes). Judge what each piece means "
    "and what it costs, not how the code is written; a default left out is not an error.\n\n"
    "Agree only with what you have seen on the page. When you disagree, say exactly why "
    "and what the page shows instead. Hand in with submit_check."
)

SUBMIT = {
    "type": "object",
    "properties": {
        "reading": {"type": "object", "properties": {
            "agree": {"type": "boolean"},
            "why": {"type": "string", "description": "Piece by piece where you disagree"}},
            "required": ["agree", "why"]},
        "parts": {"type": "array", "description": "One entry for every part of every option",
                  "items": {"type": "object", "properties": {
                      "option": {"type": "integer", "description": "Option number, from 1"},
                      "part": {"type": "integer", "description": "Part number within the option, from 1"},
                      "agree": {"type": "boolean"},
                      "why": {"type": "string"}},
                      "required": ["option", "part", "agree", "why"]}},
        "missed": {"type": "array", "items": {"type": "string"},
                   "description": "What the pages say that the answer misses"},
    },
    "required": ["reading", "parts"],
}


@dataclass
class PartCheck:
    agree: bool
    why: str = ""


@dataclass
class Checked:
    """Agent 3's word on Agent 2's answer. `done` false: it gave none."""

    model: str
    done: bool
    reading_agrees: bool = False
    reading_why: str = ""
    parts: dict[tuple[int, int], PartCheck] = field(default_factory=dict)   # (option, part), from 1
    missed: list[str] = field(default_factory=list)
    note: str = ""
    outcome: agent.Outcome | None = None

    def option_agrees(self, option: int, parts: int) -> bool:
        """Every part of this option checked and agreed (an unchecked part does not pass)."""
        return all((option, p) in self.parts and self.parts[(option, p)].agree for p in range(1, parts + 1))


def answer_text(priced: Priced, shelf: book_skills.Shelf) -> str:
    labels = {name: label for label, name in shelf.books.items()}
    out = [f"Status: {priced.status}"
           + ("" if priced.orderable else "; the book says the code cannot be ordered as written")]
    for i, (label, read) in enumerate(priced.options, 1):
        out.append(f"Option {i}: {label or '(the only one)'}"
                   + (f" | size: {read.product.size}" if read.product.size else ""))
        for j, p in enumerate(read.parts, 1):
            out.append(f"  Part {j}: {p.label} | {labels.get(p.book, p.book)} p.{p.pdf_page} | "
                       f"{p.price_text} per {p.per} x {p.units:g} | read from: {p.row}")
    out.extend(f"Note: {n}" for n in priced.notes)
    out.extend(f"Needs from a person: {n}" for n in priced.needs_from_person)
    return "\n".join(out)


def _pages(priced: Priced, shelf: book_skills.Shelf) -> set[tuple[str, int]]:
    """The pages Agent 3 may open: every page cited, and every page Agent 2 read."""
    labels = {name: label for label, name in shelf.books.items()}
    out = {(labels[p.book], p.pdf_page) for _, r in priced.options for p in r.parts if p.book in labels}
    if priced.outcome is not None:
        for t in priced.outcome.turns:
            if t.skill in ("read_page", "page_image") and str(t.args.get("book", "")).upper() in shelf.books:
                out.update((str(t.args["book"]).upper(), p) for p in t.pages)
    return out


async def check(priced: Priced, manufacturer: str, shelf: book_skills.Shelf, model: str,
                guide: str = "", on_turn: agent.OnTurn | None = None) -> Checked:
    """Run Agent 3 on Agent 2's answer for one product."""
    if not priced.options or not any(r.parts for _, r in priced.options):
        return Checked(model, False, note="Nothing to check: no price was handed in")
    held = book_skills.Shelf(shelf.books, shelf.maps, only=_pages(priced, shelf))
    expected = {(i, j) for i, (_, r) in enumerate(priced.options, 1) for j in range(1, len(r.parts) + 1)}

    async def submit(args: dict[str, Any]) -> Reply:
        given = {(int(p.get("option", 0)), int(p.get("part", 0))) for p in args.get("parts") or []
                 if isinstance(p, dict)}
        missing = sorted(expected - given)
        if missing:
            return Reply("Not accepted: check every part. Missing: "
                         + ", ".join(f"option {o} part {p}" for o, p in missing), error=True)
        return Reply("Handed in.")

    task = (f"The product, for ONE unit:\n  {priced.product.text()}\nManufacturer: {manufacturer}\n\n"
            f"The books:\n{shelf.listing()}\n\n"
            f"Book guide:\n{guide.strip() or '(none: open the cited pages to see how codes are built)'}\n\n"
            f"Your colleague's answer:\n{answer_text(priced, shelf)}\n\n"
            "Pages you may open: " + ", ".join(f"{b} p.{p}" for b, p in sorted(held.only or [])))
    skills = book_skills.skills(held) + [
        Skill("submit_check", "Hand in your check of the reading and of every part. Ends the work.",
              SUBMIT, submit, final=True)]
    outcome = await agent.run(step=STEP, model=model, system=SYSTEM, task=task, skills=skills,
                              max_steps=MAX_STEPS, max_images=MAX_IMAGES, manufacturer=manufacturer,
                              books=list(shelf.books.values()),
                              product_keys=[priced.product.product_key], on_turn=on_turn)
    if outcome.answer is None:
        return Checked(model, False, note=outcome.note or "The checker gave no answer", outcome=outcome)
    a = outcome.answer
    reading = a.get("reading") if isinstance(a.get("reading"), dict) else {}
    return Checked(
        model, True, bool(reading.get("agree")), str(reading.get("why", ""))[:600],
        {(int(p["option"]), int(p["part"])): PartCheck(bool(p.get("agree")), str(p.get("why", ""))[:300])
         for p in a.get("parts") or [] if isinstance(p, dict) and "option" in p and "part" in p},
        [str(m)[:300] for m in a.get("missed") or []][:8], outcome=outcome)
