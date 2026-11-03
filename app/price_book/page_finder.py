"""Step A of pricing with AI: which pages of a maker's books price each product.

One text-only call per maker. The AI is shown that maker's book maps -- one
line per page, heading, number of prices and models priced there -- and every
distinct product the project has from that maker, and names up to three pages
for each. No page image is sent here; step B reads the pages named.

Code does what code can, before and after:

  before  pages that print no price are left out of the map (a price cannot be
          read from them), and where a book prints its own part-number index
          the pages it gives for a product are shown as a hint
  after   a page named must exist in that maker's books and print prices (or
          be a picture page, whose prices only an image read can see); any
          other is dropped. Each kept page says whether the product's model is
          among the models the map lists for it.

A product with no page kept is reported, not guessed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.price_book import call_log
from app.price_book.book_map import IndexEntry, PageEntry
from app.price_book.makers import Written
from app.price_book.rows import match_key

log = logging.getLogger(__name__)

# Pages named per product. A model priced across a table that runs over a
# page break needs two; a lock built from parts (base, trim, options) three.
MAX_PAGES = 3
# Products per call. Each answer is a few dozen tokens; this keeps the reply
# well inside what a model returns in one go, and the map is sent per call.
BATCH = 40
# Room for the reply. Models that think before answering (Haiku 5.5, Gemini
# 3.8) spend their reasoning from the same allowance: at 300 + 120 a product
# they ran out before writing the answer and returned nothing.
REPLY_TOKENS = 8000


class CutOff(RuntimeError):
    """The model stopped at its token limit: its answer is missing, not empty."""


@dataclass
class Product:
    """One distinct product of a maker, and the items that are it."""

    key: str
    model: str
    finish: str
    size: str
    description: str
    name: str = ""
    item_ids: list[str | None] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    # Model, finish and size matched on letters and digits: what a stored AI
    # price is found by in the next project.
    product_key: str = ""
    door_width: float | None = None

    def text(self) -> str:
        parts = [self.name, self.model, self.description,
                 f"finish {self.finish}" if self.finish else "",
                 f"size {self.size}" if self.size else "",
                 # LDW plates and LAR thresholds are sized from the door; without
                 # it both models called a kick plate unclear.
                 f"door width {self.door_width:g}\"" if self.door_width else ""]
        return " | ".join(p for p in parts if p)


def products(items: list[Written]) -> list[Product]:
    """The distinct products among a maker's items, in the order first seen."""
    out: dict[tuple[str, str, str], Product] = {}
    for i in items:
        key = i.product()
        if key not in out:
            out[key] = Product(f"P{len(out) + 1}", i.model, i.finish, i.size, i.description,
                               i.name, candidates=i.candidates(), product_key="|".join(key),
                               door_width=i.door_width)
        out[key].item_ids.append(i.id)
    return list(out.values())


@dataclass
class Book:
    """A book as step A sees it: its map, and its index if it prints one."""

    file_name: str
    pages: list[PageEntry]
    index: list[IndexEntry] = field(default_factory=list)

    def priced_pages(self) -> list[PageEntry]:
        return [p for p in self.pages if p.prices or not p.has_text]


@dataclass
class PagePick:
    book: str
    pdf_page: int
    printed_page: str
    heading: str
    model_on_page: bool
    why: str = ""


@dataclass
class Found:
    product: Product
    pages: list[PagePick] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)   # pages named that were not kept, and why


@dataclass
class StepA:
    manufacturer: str
    found: list[Found]
    model: str
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usd: float = 0.0


_SYSTEM = (
    "You find where a door hardware product is priced in a manufacturer's price "
    "book, using the book's table of contents. You only name pages listed in it, "
    "and name none when no page fits."
)


def _index_hints(books: list[Book], product: Product) -> list[str]:
    keys = {match_key(c) for c in product.candidates} | {match_key(product.model)}
    keys.discard("")
    out = []
    for n, b in enumerate(books, 1):
        for e in b.index:
            if match_key(e.model) in keys and e.pdf_page:
                out.append(f"B{n} p{e.pdf_page}")
    return sorted(set(out))[:MAX_PAGES]


def _prompt(maker: str, books: list[Book], batch: list[Product]) -> str:
    maps = []
    for n, b in enumerate(books, 1):
        lines = "\n".join(p.line() for p in b.priced_pages())
        maps.append(f"=== B{n}: {b.file_name} ===\n{lines}")
    listing = []
    for p in batch:
        hint = _index_hints(books, p)
        listing.append(f"  {p.key}: {p.text()}" + (f"  (book index: {', '.join(hint)})" if hint else ""))
    return (
        f"Price books of {maker}. Each line is one page: page number, [printed page "
        "number], heading, how many prices it prints, and model numbers priced on it.\n\n"
        + "\n\n".join(maps)
        + "\n\nProducts from a hardware schedule:\n" + "\n".join(listing) + "\n\n"
        "For each product, name the page or pages where its list price is printed.\n"
        "Rules:\n"
        f"- At most {MAX_PAGES} pages per product, best first. Use more than one only "
        "when the price is built from parts on different pages (a device and its "
        "trim, a lock and its options) or a table runs onto the next page.\n"
        "- The product's name (Hinge, Closer, Mortise Cylinder, Exit Device) says "
        "which section to look in. Go by the heading and the series as much as the "
        "model list: a model number also appears on pages about other products, "
        "in notes such as \"requires 3902 mortise cylinder\".\n"
        "- A model number is often built from parts: a prefix option (F for fire "
        "rated), the series (10, 4500), a trim or function code, then suffix "
        "options (LD, W). Then name the page of the series' device price first, "
        "then the pages of its trim or options.\n"
        "- Every suffix after the base model that is an option (NRP, ETW, EPT, SLC, "
        "HDCS, FEC) is priced somewhere, often on a modifications or options page of "
        "its section (\"Non-Removable Pins\", \"Electrified Options\"): name that page "
        "too, within the page limit.\n"
        "- Some suffixes are schedule shorthand, not options, and have no page: LAR "
        "(length as required), LDW (less door width), \"As Req'd\", a length or opening "
        "size. They need no page of their own; still name the product's page.\n"
        "- Where a section says how its prices are figured on a page of its own (door "
        "plates: \"multiply the square inch size by the list price\"; thresholds: "
        "\"U/M is feet\"), name that page too.\n"
        "- Page numbers are the p-numbers above, not the printed [numbers].\n"
        "- If no page fits, return an empty list. A wrong page wastes a read; a "
        "missing one is sent to a person.\n\n"
        'Return JSON: {"products": {"<P-key>": [{"book": "B1", "page": 147, '
        '"why": "<a few words>"}]}}'
    )


_KEY = re.compile(r'"(P\d+)"\s*:')
_PICK = re.compile(r'"book"\s*:\s*"?(B?\d+)"?\s*,\s*"page"\s*:\s*"?p?(\d+)"?(?:\s*,\s*"why"\s*:\s*"([^"]*)")?')


def salvage(content: str | None) -> dict[str, list[dict]]:
    """Each product's pages read out of a reply that is not valid JSON.

    Gemini 2.5 Flash has written a stray quote where a bracket belongs ("}

    ",") in an answer that was otherwise right; one such character lost forty
    products' pages. The answer's shape is simple enough to read piece by
    piece: a product key, then the book and page pairs up to the next key.
    """
    text = content or ""
    keys = list(_KEY.finditer(text))
    out: dict[str, list[dict]] = {}
    for n, k in enumerate(keys):
        end = keys[n + 1].start() if n + 1 < len(keys) else len(text)
        out[k.group(1)] = [{"book": b, "page": int(p), "why": w or ""}
                           for b, p, w in _PICK.findall(text[k.end():end])]
    return out


def _check(found: Found, raw: object, books: list[Book]) -> None:
    """Keep the pages named that are real price pages of these books."""
    if not isinstance(raw, list):
        return
    keys = {match_key(c) for c in found.product.candidates}
    for pick in raw[:MAX_PAGES]:
        if not isinstance(pick, dict):
            continue
        label, page = str(pick.get("book", "")).strip().upper(), pick.get("page")
        # Models write the page as 21, "21" or "p21", and a lone book as "1".
        page_text = str(page).strip().lower().lstrip("p")
        page = int(page_text) if page_text.isdigit() else None
        digits = label[1:] if label[:1] == "B" else label
        n = int(digits) if digits.isdigit() else 0
        if not 1 <= n <= len(books) or page is None:
            found.dropped.append(f"{label} p{page}: not a page of these books")
            continue
        book = books[n - 1]
        entry = next((p for p in book.pages if p.pdf_page == page), None)
        if entry is None:
            found.dropped.append(f"{label} p{page}: no such page")
            continue
        if entry.has_text and not entry.prices:
            found.dropped.append(f"{label} p{page}: prints no prices")
            continue
        if any(p.book == book.file_name and p.pdf_page == page for p in found.pages):
            continue
        found.pages.append(PagePick(
            book.file_name, page, entry.printed_page, entry.heading,
            bool(keys & {match_key(m) for m in entry.models}), str(pick.get("why", ""))[:200]))


async def find_pages(maker: str, books: list[Book], items: list[Written], model: str) -> StepA:
    """Name the pages that price each of a maker's products. Raises
    AiUnavailableError when no key is set."""
    from app.ai import spend
    from app.ai.client import AiUnavailableError, get_client
    from app.ai.pricing import cost_usd
    from app.ai.response_parser import first_object

    wanted = products(items)
    result = StepA(maker, [Found(p) for p in wanted], model)
    by_key = {f.product.key: f for f in result.found}
    for start in range(0, len(wanted), BATCH):
        batch = wanted[start:start + BATCH]
        timer = call_log.Timer()
        try:
            response = await get_client().chat.completions.create(
                model=model, temperature=0, max_tokens=REPLY_TOKENS + 120 * len(batch),
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": _SYSTEM},
                          {"role": "user", "content": _prompt(maker, books, batch)}],
            )
        except AiUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 - this batch is lost, not the maker
            log.warning("step A %s: a call failed: %s", maker, exc)
            call_log.add(call_log.Call("A", model, maker, [b.file_name for b in books], [],
                                       [p.product_key for p in batch], 0, 0, 0.0, timer.seconds, "error"))
            for p in batch:
                by_key[p.key].dropped.append(f"The page search failed ({type(exc).__name__}); run it again")
            continue
        usage = getattr(response, "usage", None)
        spend.add(model, usage)
        prompt = getattr(usage, "prompt_tokens", 0) or 0
        completion = getattr(usage, "completion_tokens", 0) or 0
        result.calls += 1
        result.prompt_tokens += prompt
        result.completion_tokens += completion
        result.usd += cost_usd(model, prompt, completion)
        cut = response.choices[0].finish_reason == "length"
        call_log.add(call_log.Call("A", model, maker, [b.file_name for b in books], [],
                                   [p.product_key for p in batch], prompt, completion,
                                   cost_usd(model, prompt, completion), timer.seconds,
                                   "cut_off" if cut else "ok"))
        if cut:
            raise CutOff(f"{model} stopped at its token limit before answering")
        content = response.choices[0].message.content
        raw = first_object(content).get("products") or salvage(content)
        for p in batch:
            _check(by_key[p.key], raw.get(p.key) if isinstance(raw, dict) else None, books)
    log.info("step A %s: %d products, %d calls, tokens %d/%d", maker, len(wanted),
             result.calls, result.prompt_tokens, result.completion_tokens)
    return result
