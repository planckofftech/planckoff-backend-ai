"""Agent 2: price one product the way an estimator with the book open would.

Given the product as the schedule writes it, the maker's books and the book
guide where one is made, the agent works out what the code is made of, finds
the base price, follows the book to every add-on, reads the rules on the pages
it uses, and hands in the parts: each price as printed, with the page it is on.

What it hands in is not trusted as it stands. Each option becomes a step B
`Read`, so the checks every AI price goes through apply unchanged: every
amount must be printed on the page named, a rate read as the price of one is
caught, units are counted from the schedule, and code adds the parts. The
agent never sends a total.

A code that leaves a price-changing choice open (no trim letter, no keyway)
comes back as options, one per choice, with what a person must specify. A code
the book says cannot be ordered comes back priced as written where the book
prints it, with the book's words.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from app.price_book import agent, book_skills
from app.price_book.agent import Reply, Skill
from app.price_book.page_finder import Product
from app.price_book.price_reader import Part, Read, _amount, _by_length, _LENGTH
from app.price_book import measure

STEP = "agent:price"
MAX_STEPS = 24
MAX_OPTIONS = 4

SYSTEM = (
    "You price door hardware from manufacturers' price books, the way an experienced "
    "estimator does with the book open. You have skills to look through the books: their "
    "contents, a search over every page, a page's text and a page's picture. Use them as "
    "often as you need, then hand in with submit_price.\n\n"
    "How to work:\n"
    "1. Work out what the product code is made of, piece by piece, in the order the book "
    "(or the book guide) says an order code is written: a letter means different things "
    "in different places, so place each one before deciding what it is. Many books build "
    "a price from parts: a "
    "base product priced by function, design or trim, and finish, plus add-ons for "
    "cylinders, keyways, options, indicators and the like. The book's 'How to order' or "
    "'Configure a product' pages say how its codes are built; read them unless the book "
    "guide already says.\n"
    "2. Find the base price: the product's row, in the column for its design or trim and "
    "its finish.\n"
    "3. For every other piece of the code, find what it adds: search for it, open the page, "
    "read the price. Follow the book's own pointers ('see page 177'): those are printed "
    "page numbers, and the skills take PDF page numbers; contents and search show both.\n"
    "4. Read the rules and footnotes on the pages you use: what is included at no charge, "
    "what is not available with what, what must also be specified.\n"
    "5. Hand in with submit_price.\n\n"
    "Rules for the answer:\n"
    "- Copy every price exactly as printed. Never estimate, never apply a discount, never "
    "add up: code adds the parts.\n"
    "- Each part names its book label, the PDF page it is printed on, and the row and "
    "column you read it from.\n"
    "- A piece of the code that costs nothing (included, N/C, $0.00) is not a part; say so "
    "in notes.\n"
    "- If the code leaves open a choice that changes the price (a trim letter, a keyway), "
    "set status options, give one option per choice, each with all its parts, and say in "
    "needs_from_person what has to be specified. Look up each choice's price before deciding "
    "a choice makes no difference: rose and escutcheon trim are often on different pages at "
    "different prices.\n"
    "- If the book says the code cannot be ordered as written (a piece not available with "
    "another), set orderable false, quote the book's words with the page in notes, and "
    "still price it as written where the book prints the prices.\n"
    "- If you cannot find the price of a piece, do not guess: leave it out, set status "
    "unclear and say in notes what is missing.\n"
    "- Prices are list prices for ONE unit. A price per foot or per area is a rate: give "
    "per ('foot', '100 sq in') and units from the size the schedule writes.\n"
    "- When the price depends on a size the schedule does not give (a plate's width, a "
    "length 'as required', the feet of head and jambs), do not stop: set status options and "
    "give one option per size, each with all its parts and its size in square brackets in "
    "`size` ('[10\" x 34\"]' for a plate, '[36\"]' for a length). Use the sizes the book "
    "prints ready-priced or sells in stock lengths; when it prints none, use common door "
    "sizes (3'-0\", 3'-6\", 4'-0\" wide, 7'-0\" high), lengths rounded up to whole feet. "
    "Say in needs_from_person which size has to be chosen.\n"
    "- A product sold only by the pack or box (silencers in a pack of 100) is priced by the "
    "pack: the pack price is the price, per 'pack of 100', units 1, status priced. Never "
    "split it into a price per piece; say the pack size in notes.\n"
    "- B4E on a plate (kick, mop, armor, push) is bevelled 4 edges, the standard plate: "
    "written or not, the plate's price includes it. Add a charge for it only where the book "
    "prints one; a book that never mentions it is no reason for status unclear.\n"
    "- Finish names have equivalents: US26D = 626, US32D = 630, US3 = 605, US4 = 606, "
    "US10B = 613, US15 = 619, US26 = 625, US28 = 628, US19/BLK/BLACK = black.\n"
    "- Be economical: when the guide or the contents says where a section is, search "
    "inside it with first_page and last_page."
)

_PART = {
    "type": "object",
    "properties": {
        "what": {"type": "string", "description": "What this part is, e.g. 'L9050 base lock, ME3 lever, 643e'"},
        "book": {"type": "string", "description": "Book label, e.g. B1"},
        "page": {"type": "integer", "description": "PDF page the price is printed on"},
        "row": {"type": "string", "description": "The row and column the price was read from"},
        "price_text": {"type": "string", "description": "The price exactly as printed, e.g. '$1,525'"},
        "amount": {"type": "number", "description": "The same price as a number"},
        "per": {"type": "string", "description": "'each', or the unit of a rate ('foot', '100 sq in')"},
        "units": {"type": "number", "description": "How many of `per`; 1 for each"},
    },
    "required": ["what", "book", "page", "price_text", "amount"],
}

SUBMIT = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["priced", "options", "unclear", "not_found"],
                   "description": "priced: every piece of the code was found, one option; options: every "
                                  "piece was found but the code leaves a choice open, one option per "
                                  "choice; unclear: a piece is missing or a size is needed; not_found: "
                                  "the product is not in the books"},
        "options": {"type": "array", "maxItems": MAX_OPTIONS,
                    "description": "One option normally; one per open choice that changes the price",
                    "items": {"type": "object", "properties": {
                        "label": {"type": "string", "description": "'' for a single option, else the choice, "
                                                                     "e.g. 'Rose trim (A, B or C)'"},
                        "size": {"type": "string", "description": "The size this option assumes when the "
                                                                   "schedule gives none, in square brackets: "
                                                                   "'[10\" x 34\"]', '[36\"]'; else ''"},
                        "parts": {"type": "array", "items": _PART}},
                        "required": ["label", "parts"]}},
        "orderable": {"type": "boolean", "description": "false when the book says the code cannot be ordered as written"},
        "notes": {"type": "array", "items": {"type": "string"},
                  "description": "Rules, inclusions, what is missing, with pages"},
        "needs_from_person": {"type": "array", "items": {"type": "string"},
                              "description": "What a person must specify or decide"},
    },
    "required": ["status", "options", "notes"],
}


@dataclass
class Priced:
    """Agent 2's answer for one product, its options read as step B reads."""

    product: Product
    status: str                                   # priced | options | unclear | not_found | no_answer
    options: list[tuple[str, Read]] = field(default_factory=list)
    orderable: bool = True
    notes: list[str] = field(default_factory=list)
    needs_from_person: list[str] = field(default_factory=list)
    outcome: agent.Outcome | None = None


def task(product: Product, manufacturer: str, shelf: book_skills.Shelf, guide: str) -> str:
    return (f"Price this product from a hardware schedule, for ONE unit:\n  {product.text()}\n"
            f"Manufacturer: {manufacturer}\n\nThe books:\n{shelf.listing()}\n\n"
            "Book guide:\n" + (guide.strip() or "(none yet: use the contents and the book's "
                                                 "'How to order' pages)"))


def _check(args: dict[str, Any], shelf: book_skills.Shelf) -> list[str]:
    """What is wrong with a submit_price call, told back to the agent to fix."""
    wrong = []
    options = args.get("options")
    if not isinstance(options, list):
        return ["options must be a list"]
    if args.get("status") in ("priced", "options") and not any(isinstance(o, dict) and o.get("parts") for o in options):
        wrong.append("status is priced but no option has parts")
    for o in options:
        for p in (o.get("parts") or []) if isinstance(o, dict) else []:
            label = str(p.get("book", "")).upper()
            if label not in shelf.books:
                wrong.append(f"part '{p.get('what')}' names book {p.get('book')!r}, not one of {', '.join(shelf.books)}")
            elif not isinstance(p.get("page"), int) or p["page"] < 1:
                wrong.append(f"part '{p.get('what')}' has no PDF page")
            if _amount(p.get("amount")) is None:
                wrong.append(f"part '{p.get('what')}' has no amount")
    return wrong


def _option_size(option: dict) -> str:
    """The size an option assumes, bracketed: measure reads a bracketed size as
    the plate as cut, or the length, so the count is still worked out in code."""
    size = str(option.get("size") or "").strip()[:40]
    if not size:
        return ""
    return size if size.startswith("[") else f"[{size.strip('[]')}]"


def _read(product: Product, status: str, option: dict, shelf: book_skills.Shelf) -> Read:
    # A size the schedule leaves out is a choice: each option is priced and
    # checked at the size it names, without touching the schedule's own size.
    size = _option_size(option)
    if size and status == "options":
        product = replace(product, size=f"{product.size} {size}".strip())
    # Each option of an open choice is itself a complete price: read as priced.
    out = Read(product, "priced" if status in ("priced", "options") else "unclear")
    for p in option.get("parts") or []:
        units = _amount(p.get("units", 1))
        out.parts.append(Part(str(p.get("what", ""))[:120], shelf.books[str(p["book"]).upper()], int(p["page"]),
                              str(p.get("price_text", ""))[:40], _amount(p["amount"]) or 0.0,
                              str(p.get("row", ""))[:200], str(p.get("per", "each"))[:20] or "each",
                              units if units and units > 0 else 1))
    written = f"{product.description} {product.model} {product.size}"
    if out.status == "priced" and _by_length(out.parts) and not _LENGTH.search(written) \
            and measure.length_units(written, product.door_width) is None:
        out.status = "unclear"
        out.note = "Priced by length, and the schedule gives no length."
    if out.status == "priced" and not out.parts:
        out.status = "unclear"
    out.total = round(sum(p.extended for p in out.parts), 2) if out.status == "priced" else None
    return out


async def price(product: Product, manufacturer: str, shelf: book_skills.Shelf, model: str,
                guide: str = "", on_turn: agent.OnTurn | None = None) -> Priced:
    """Run Agent 2 on one product."""
    async def submit(args: dict[str, Any]) -> Reply:
        wrong = _check(args, shelf)
        if wrong:
            return Reply("Not accepted, fix and hand in again: " + "; ".join(wrong), error=True)
        return Reply("Handed in.")

    skills = book_skills.skills(shelf) + [
        Skill("submit_price", "Hand in the price: its parts with pages, options, notes. Ends the work.",
              SUBMIT, submit, final=True)]
    outcome = await agent.run(step=STEP, model=model, system=SYSTEM,
                              task=task(product, manufacturer, shelf, guide), skills=skills,
                              max_steps=MAX_STEPS, manufacturer=manufacturer,
                              books=list(shelf.books.values()), product_keys=[product.product_key],
                              on_turn=on_turn)
    if outcome.answer is None:
        return Priced(product, "no_answer", notes=[outcome.note] if outcome.note else [], outcome=outcome)
    a = outcome.answer
    status = a.get("status") if a.get("status") in ("priced", "options", "unclear", "not_found") else "unclear"
    options = [o for o in a.get("options") or [] if isinstance(o, dict)][:MAX_OPTIONS]
    return Priced(product, status,
                  [(str(o.get("label") or _option_size(o).strip("[]"))[:120], _read(product, status, o, shelf))
                   for o in options],
                  bool(a.get("orderable", True)),
                  [str(n)[:400] for n in a.get("notes") or []][:12],
                  [str(n)[:300] for n in a.get("needs_from_person") or []][:6],
                  outcome)
