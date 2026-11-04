"""Agent 1: learn how a price book is organised, once, and write it down.

The pricing agent read `LV9495EUL 42 630` with its L as L-escutcheon trim. The
book's own "How to order" page puts the cylinder straight after the function,
so that L is "less conventional cylinder", -$268. Every price it gave was
printed on its page; the code was read in the wrong order. A guide that says the
order, and where each kind of price is, is what it should have started from --
and it is the same for every product priced from that book, so it is written
once per book and kept (price_book_guides), not worked out again per product.

For each product family the book prices, the guide holds:

  pages        where the family is
  code_order   the order the pieces of an order code come in, as the book says
  base_prices  where the base price tables are and what picks the column
  add_ons      where each kind of add-on is priced (cylinders, options...)
  rules        what is included, what is not available with what

and general notes for the whole book. A person can correct it; a corrected
guide is never replaced by the agent.

Each family is saved as it is finished (save_family), and finish_guide ends
the work: the first run wrote all sixteen Schlage families in one reply, which
was cut off at the reply limit and lost. A guide can also be made for one
section only (`pages`), which is all one product needs and costs a fraction
of the whole book.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.price_book import agent, book_skills
from app.price_book.agent import Reply, Skill

STEP = "agent:guide"
MAX_STEPS = 40
MAX_IMAGES = 4

SYSTEM = (
    "You study a door hardware manufacturer's price book and write a short guide to it "
    "for a colleague who will price products from it. The colleague has the same skills "
    "you have (contents, search, read a page, see a page) and will look up the prices "
    "themselves: the guide tells them how the book works, not what things cost.\n\n"
    "How to work:\n"
    "1. Read the contents (in parts of up to 120 pages) to see the product families the "
    "book prices and where each one is.\n"
    "2. For each family that has price pages, find its 'How to order', 'How to "
    "specify' or 'Configure a product' pages and read them, and look at one or two of "
    "its price pages.\n"
    "3. Save each family with save_family as soon as you have read it, then end with "
    "finish_guide.\n\n"
    "What the guide must get right:\n"
    "- code_order: the order the pieces of an order code come in, exactly as the book "
    "gives it (for example: series and function, then cylinder type, then lever design, "
    "then rose or escutcheon, then finish), and which letters or codes go in each place. "
    "A letter means different things in different places, so say what each position "
    "takes.\n"
    "- Pages: PDF page numbers, the ones the skills use, with the printed page numbers "
    "where they differ.\n"
    "- base_prices: which pages hold the base prices, and what picks the row and the "
    "column (function, design group, trim type, finish group).\n"
    "- add_ons: each kind of add-on (cylinders and keyways, options, monitoring, "
    "indicators, strikes, finishes), with its pages.\n"
    "- rules: what is included at no charge, what is not available with what, what must "
    "be specified, as the book words it, with the page.\n"
    "Copy the book; do not guess. Leave out what you did not read. Keep each family "
    "short: the colleague reads the pages themselves."
)

_FAMILY = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "e.g. 'L Series mortise locks'"},
        "pages": {"type": "string", "description": "PDF pages, e.g. '140-225 (printed 138-223)'"},
        "how_to_order_pages": {"type": "string"},
        "code_order": {"type": "string", "description": "The order of an order code's pieces and what each takes"},
        "base_prices": {"type": "string"},
        "add_ons": {"type": "array", "items": {"type": "string"},
                    "description": "One line per kind of add-on, with its pages"},
        "rules": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["name", "pages", "code_order", "base_prices"],
}

FINISH = {
    "type": "object",
    "properties": {
        "general": {"type": "array", "items": {"type": "string"},
                    "description": "Notes for the whole book: list prices, finish groups, how pages are numbered"},
    },
    "required": ["general"],
}


@dataclass
class Made:
    guide: dict[str, Any] | None
    outcome: agent.Outcome


def merge(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """A guide made for one section added to the book's guide: its families
    replace those of the same name, the rest are kept."""
    if not old:
        return new
    names = {f.get("name") for f in new.get("families") or []}
    return {"general": list(dict.fromkeys((old.get("general") or []) + (new.get("general") or []))),
            "families": [f for f in old.get("families") or [] if f.get("name") not in names]
            + list(new.get("families") or [])}


def render(guide: dict[str, Any] | None) -> str:
    """The guide as the pricing agent reads it."""
    if not guide:
        return ""
    out = [f"- {g}" for g in guide.get("general") or []]
    for f in guide.get("families") or []:
        out.append(f"\n## {f.get('name', '')}  (pages {f.get('pages', '')})")
        if f.get("how_to_order_pages"):
            out.append(f"How to order: pages {f['how_to_order_pages']}")
        out.append(f"Code order: {f.get('code_order', '')}")
        out.append(f"Base prices: {f.get('base_prices', '')}")
        out.extend(f"Add-on: {a}" for a in f.get("add_ons") or [])
        out.extend(f"Rule: {r}" for r in f.get("rules") or [])
    return "\n".join(out).strip()


async def make(file_name: str, model: str, shelf: book_skills.Shelf | None = None,
               pages: tuple[int, int] | None = None, on_turn: agent.OnTurn | None = None) -> Made:
    """Run Agent 1 over one book, or over the section on `pages` (PDF, first and last)."""
    shelf = shelf or book_skills.Shelf.of([file_name])
    families: list[dict[str, Any]] = []

    async def save_family(args: dict[str, Any]) -> Reply:
        if not args.get("name") or not args.get("code_order") or not args.get("base_prices"):
            return Reply("Not saved: a family needs its name, code_order and base_prices.", error=True)
        families[:] = [f for f in families if f.get("name") != args["name"]] + [args]
        return Reply(f"Saved {args['name']} ({len(families)} so far).")

    async def finish(args: dict[str, Any]) -> Reply:
        if not families:
            return Reply("Nothing to finish: save at least one family first.", error=True)
        return Reply("Guide finished.")

    skills = book_skills.skills(shelf) + [
        Skill("save_family", "Save one product family of the guide. Saving a family again "
              "replaces it.", _FAMILY, save_family),
        Skill("finish_guide", "End the work, with notes for the whole book.", FINISH, finish, final=True)]
    count = len(shelf.maps["B1"].pages) if "B1" in shelf.maps else 0
    if pages:
        scope = (f"Write the guide to the product family on PDF pages {pages[0]}-{pages[1]} only: "
                 "read the contents of those pages first, and stay inside them except where "
                 "they point to a page outside.")
    elif count:
        scope = f"Write the guide to the whole book. It has {count} pages; read its contents first."
    else:
        scope = "Write the guide to the whole book. It has no contents; search it for 'How to order'."
    outcome = await agent.run(step=STEP, model=model, system=SYSTEM,
                              task=f"The book:\n{shelf.listing()}\n\n{scope}", skills=skills,
                              max_steps=MAX_STEPS, max_images=MAX_IMAGES,
                              books=list(shelf.books.values()), on_turn=on_turn)
    if not families:
        return Made(None, outcome)
    general = (outcome.answer or {}).get("general") or []
    # Families saved before the agent stopped are kept, finish_guide or not.
    return Made({"general": [str(g) for g in general], "families": families}, outcome)
