"""Step B of pricing with AI: read each product's list price off its pages.

Step A named the pages; here the AI sees them. Products that share pages are
read in one call, so ten Hager items on two closer pages cost one call, not
ten. Each page goes as an image, for the layout -- which column is which
finish, which rows a model heads -- and with its text layer, where the page has
one, for the digits: a price copied from the text is the one printed, not a
reading of pixels.

The answer for a product is the parts its price is made of, each with the page
and the price exactly as printed: a hinge is one part; a Detex F 1009DU is the
10 Series device, the fire-rated option and the trim. Nothing here decides
whether to believe it -- `check.py`-style verification against the page comes
after (task 5) -- but a reply that is cut off or not JSON reads as no answer,
never as a price.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from dataclasses import dataclass, field

import fitz

from app.price_book import call_log, measure, page_text, pdfs
from app.price_book.page_finder import Found, PagePick, Product

log = logging.getLogger(__name__)

# Pages and products per call. Pages are what cost: each is an image and its
# text. Four covers a device, its trim and its options with room to spare.
MAX_PAGES = 4
MAX_PRODUCTS = 15
# Price books print down to 5 pt (Von Duprin's finish columns); at 110 dpi that
# is still about 8 px tall, enough to read with the text layer beside it.
DPI = 110
REPLY_TOKENS = 8000


# A length written on a schedule: 36", 3', 19 LF, 20 FT.
_LENGTH = re.compile(r"\d\s*(\"|'|(LF|FT|FEET|FOOT)(?![A-Z]))", re.I)
_EACH = {"each", "ea", "set", "pair", "pr", "kit", ""}


def _by_length(parts: list["Part"]) -> bool:
    return any(p.per.strip().lower() not in _EACH for p in parts)


class CutOff(RuntimeError):
    """The model stopped at its token limit: its answer is missing, not empty."""


@dataclass
class Part:
    label: str
    book: str
    pdf_page: int
    price_text: str
    amount: float
    row: str = ""
    # A price printed per foot (thresholds, weatherstrip) is bought by length:
    # `per` says so and `units` is how many (36" is 3 feet). The total is
    # worked out here, from these, never taken from the model's own sum.
    per: str = "each"
    units: float = 1

    @property
    def extended(self) -> float:
        return round(self.amount * self.units, 2)


@dataclass
class Read:
    """What the AI read for one product."""

    product: Product
    status: str                      # priced | unclear | not_on_pages
    parts: list[Part] = field(default_factory=list)
    total: float | None = None       # the parts added up here
    model_total: float | None = None # the model's own sum, kept to compare
    note: str = ""
    options: list[dict] = field(default_factory=list)


@dataclass
class StepB:
    manufacturer: str
    reads: list[Read]
    model: str
    calls: int = 0
    pages_sent: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usd: float = 0.0


@dataclass
class _Call:
    pages: list[PagePick] = field(default_factory=list)
    found: list[Found] = field(default_factory=list)

    def keys(self) -> set[tuple[str, int]]:
        return {(p.book, p.pdf_page) for p in self.pages}


def plan_calls(found: list[Found]) -> list[_Call]:
    """Products grouped so that those sharing pages are read together.

    A product joins a call it shares a page with when the pages still fit;
    otherwise it starts its own. A product step A found no page for is not read.
    """
    calls: list[_Call] = []
    for f in found:
        if not f.pages:
            continue
        mine = {(p.book, p.pdf_page) for p in f.pages}
        home = next((c for c in calls if c.keys() & mine and len(c.found) < MAX_PRODUCTS
                     and len(c.keys() | mine) <= MAX_PAGES), None)
        if home is None:
            home = _Call()
            calls.append(home)
        home.found.append(f)
        for p in f.pages:
            if (p.book, p.pdf_page) not in home.keys():
                home.pages.append(p)
    return calls


def _page_content(doc: fitz.Document, pick: PagePick, label: str) -> list[dict]:
    page = doc[pick.pdf_page - 1]
    png = page.get_pixmap(dpi=DPI).tobytes("png")
    lines = [" | ".join(p.text for p in row) for row in page_text.lines(page_text.pieces(page))]
    text = "\n".join(lines) if lines else "(no text layer: read the image)"
    return [
        {"type": "text", "text": f"=== {label}: {pick.book}, page {pick.pdf_page} "
                                 f"(printed {pick.printed_page or '-'}), {pick.heading} ===\n"
                                 f"Text layer, one printed line per line, cells split by |:\n{text}"},
        {"type": "image_url",
         "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}},
    ]


_SYSTEM = (
    "You read list prices from door hardware manufacturers' price book pages. You "
    "copy prices exactly as printed, never calculate a discount, never estimate, "
    "and say so when a price is not on the pages or is not clear."
)


def _prompt(call: _Call, labels: dict[tuple[str, int], str]) -> str:
    listing = "\n".join(f"  {f.product.key}: {f.product.text()}" for f in call.found)
    return (
        "Products from a hardware schedule, each priced for ONE unit:\n" + listing + "\n\n"
        f"The pages are {', '.join(labels.values())}, each given as its text layer and "
        "its image.\n\n"
        "For each product, give its list price as the parts it is made of.\n"
        "Rules:\n"
        "- A part is one price printed on a page: the product's row in the column of "
        "its finish (and its size, where prices depend on size), or an option, trim "
        "or size add-on its model number asks for. Most products are one part.\n"
        "- price_text is the price exactly as printed (\"$2,408\", \"68.31\"); amount is "
        "the same number. Copy it from the text layer when the page has one.\n"
        "- Finish names have equivalents: US26D = 626, US32D = 630, US3 = 605, US4 = 606, "
        "US10B = 613, US15 = 619, US26 = 625, US28 = 628, US19/BLK/BLACK = black.\n"
        "- A part of the model number that costs nothing (\"N/C\", \"No charge\") is not "
        "a part. A part of the model number you cannot find a price for: say so in note "
        "and set status unclear.\n"
        "- Schedule shorthand is not an option and has no price of its own: LAR means "
        "length as required (cut to the opening), LDW means less door width (a plate "
        "the door's width less the inches given: 10\" x 2\" LDW is 10\" high, 2\" less "
        "than the door). They say the size depends on the door: where the price depends "
        "on that size and the size is written (\"- 6'\", \"[10\" x 34\"]\") or the door "
        "width is given (10\" x 2\" LDW on a 36\" door is 10\" x 34\"), use it; where "
        "it is not written, status unclear and say the size is needed. Never pick the "
        "smallest size instead.\n"
        "- A listing or label in the model number (UL, ADA) costs nothing unless a "
        "price for it is printed.\n"
        "- Read how the page says its prices are figured. \"U/M is feet\" means every "
        "price is per foot. A door plate table priced by height band is per hundred "
        "square inches (\"multiply the square inch size by the list price\": 10\" x 23\" "
        "is 230 sq in, units 2.3; round an odd size up to the next size the book says). "
        "Such a price is a rate: give per (\"foot\", \"100 sq in\") and units from the "
        "size written. With no length or no width written, status unclear.\n"
        "- status: priced when every part was found once; unclear when the finish, size "
        "or an option is not given or matches more than one price (list them in "
        "options); not_on_pages when the product is not on these pages.\n"
        "- total is the sum of the parts' amounts when priced, else null.\n\n"
        'Return JSON: {"products": {"<P-key>": {"status": "priced", "parts": [{"label": '
        '"10 Series device, 630", "page": "B1-12", "price_text": "$2,408", "amount": 2408, '
        '"row": "<the row and column read>", "per": "each", "units": 1}], "total": 2408, "note": "", '
        '"options": [{"label": "...", "page": "B1-12", "price_text": "..."}]}}}'
    )


def _amount(value: object) -> float | None:
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def _read(found: Found, raw: object, by_label: dict[str, tuple[str, int]]) -> Read:
    if not isinstance(raw, dict):
        return Read(found.product, "unclear", note="No answer for this product")
    status = raw.get("status") if raw.get("status") in ("priced", "unclear", "not_on_pages") else "unclear"
    out = Read(found.product, status, note=str(raw.get("note", ""))[:300],
               options=[o for o in raw.get("options") or [] if isinstance(o, dict)][:10])
    for p in raw.get("parts") or []:
        if not isinstance(p, dict):
            continue
        where = by_label.get(str(p.get("page", "")))
        amount = _amount(p.get("amount"))
        if where is None or amount is None:
            out.status = "unclear"
            out.note = (out.note + " A part named a page that was not sent.").strip()
            continue
        units = _amount(p.get("units", 1))
        if units is None or units <= 0:
            out.status = "unclear"
            out.note = (out.note + " A part has no usable number of units.").strip()
            units = 1
        out.parts.append(Part(str(p.get("label", ""))[:120], where[0], where[1],
                              str(p.get("price_text", ""))[:40], amount, str(p.get("row", ""))[:200],
                              str(p.get("per", "each"))[:20] or "each", units))
    out.model_total = _amount(raw.get("total")) if raw.get("total") is not None else None
    if out.status == "priced" and not out.parts:
        out.status = "unclear"
    written = f"{found.product.description} {found.product.model} {found.product.size}"
    if out.status == "priced" and _by_length(out.parts) and not _LENGTH.search(written)             and measure.length_units(written, found.product.door_width) is None:
        # Priced per foot with no length given is a rate, not the price of one.
        out.status = "unclear"
        out.note = (out.note + " Priced by length, and the schedule gives no length.").strip()
    out.total = round(sum(p.extended for p in out.parts), 2) if out.status == "priced" else None
    return out


async def read_prices(maker: str, found: list[Found], model: str) -> StepB:
    """Read the list price of every product step A found pages for."""
    from app.ai import spend
    from app.ai.client import AiUnavailableError, get_client
    from app.ai.pricing import cost_usd
    from app.ai.response_parser import first_object

    result = StepB(maker, [], model)
    unread = [Read(f.product, "not_on_pages", note="No page was found for it") for f in found if not f.pages]
    docs: dict[str, fitz.Document] = {}
    try:
        for call in plan_calls(found):
            labels: dict[tuple[str, int], str] = {}
            books = list(dict.fromkeys(p.book for p in call.pages))
            content: list[dict] = []
            for pick in call.pages:
                label = f"B{books.index(pick.book) + 1}-{pick.pdf_page}"
                labels[(pick.book, pick.pdf_page)] = label
                if pick.book not in docs:
                    # Off the event loop: a book may first be fetched from R2
                    # (Emtek's is 74 MB), and drawing a page is CPU work.
                    path = await asyncio.to_thread(pdfs.path_of, pick.book)
                    docs[pick.book] = await asyncio.to_thread(fitz.open, path)
                content.extend(await asyncio.to_thread(_page_content, docs[pick.book], pick, label))
            content.insert(0, {"type": "text", "text": _prompt(call, labels)})
            raw = None
            for attempt in (1, 2):
                timer = call_log.Timer()
                try:
                    response = await get_client().chat.completions.create(
                        model=model, temperature=0, max_tokens=REPLY_TOKENS + 300 * len(call.found),
                        response_format={"type": "json_object"},
                        messages=[{"role": "system", "content": _SYSTEM},
                                  {"role": "user", "content": content}],
                    )
                except AiUnavailableError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one call's failure is its products' only
                    # A dropped connection after the client's own retries. Before
                    # this, one such call lost every product of the maker.
                    log.warning("step B %s: a call failed: %s", maker, exc)
                    call_log.add(call_log.Call("B", model, maker, books, [p.pdf_page for p in call.pages],
                                               [f.product.product_key for f in call.found], 0, 0, 0.0,
                                               timer.seconds, "error"))
                    break
                usage = getattr(response, "usage", None)
                spend.add(model, usage)
                prompt = getattr(usage, "prompt_tokens", 0) or 0
                completion = getattr(usage, "completion_tokens", 0) or 0
                result.calls += 1
                result.pages_sent += len(call.pages)
                result.prompt_tokens += prompt
                result.completion_tokens += completion
                result.usd += cost_usd(model, prompt, completion)
                cut = response.choices[0].finish_reason == "length"
                parsed = first_object(response.choices[0].message.content).get("products")
                call_log.add(call_log.Call("B", model, maker, books, [p.pdf_page for p in call.pages],
                                           [f.product.product_key for f in call.found], prompt, completion,
                                           cost_usd(model, prompt, completion), timer.seconds,
                                           "cut_off" if cut else ("ok" if parsed else "error")))
                if cut:
                    raise CutOff(f"{model} stopped at its token limit before answering")
                if isinstance(parsed, dict) and parsed:
                    raw = parsed
                    break
                # Not JSON (Gemini 2.5 Flash has broken a bracket): asked once more.
                log.warning("step B %s: reply %d did not parse", maker, attempt)
            if raw is None:
                result.reads.extend(Read(f.product, "unclear", note="The read failed (no usable answer); "
                                         "run it again") for f in call.found)
                continue
            by_label = {v: k for k, v in labels.items()}
            for f in call.found:
                result.reads.append(_read(f, raw.get(f.product.key) if isinstance(raw, dict) else None,
                                          by_label))
    finally:
        for d in docs.values():
            d.close()
    result.reads.extend(unread)
    log.info("step B %s: %d products, %d calls, %d pages, tokens %d/%d", maker, len(found),
             result.calls, result.pages_sent, result.prompt_tokens, result.completion_tokens)
    return result
