"""Hardware list prices from the loaded price books.

Takes hardware items in the frontend's own shape -- `manufacturer`,
`modelNumber`, `finish`, `description`, `quantity` -- so a hardware set can be
sent as it is held. Each item comes back with a status, and a price only where
one was found; see `app.price_book.lookup` for what each status means.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import require_api_key
from app.db.client import NoDatabase
from app.ai.client import AiUnavailableError
from app.config import get_settings
from app.price_book import agent_pricing, ai_pricing, call_log, makers, measure, page_finder, search, store
from app.price_book.lookup import Item, manufacturer_of, quote

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["prices"], dependencies=[Depends(require_api_key)])

# A hardware set is tens of items; a whole project a few thousand. One request
# is one set or a few, and anything far larger is a mistake worth refusing.
_MAX_ITEMS = 2000


class PriceItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str | None = Field(None, description="Echoed back, to match answers to items")
    manufacturer: str
    model_number: str = Field("", alias="modelNumber")
    finish: str = ""
    size: str = Field("", description="Device length (4') or hinge size (4-1/2 x 4-1/2)")
    description: str = ""
    name: str = Field("", alias="itemName",
                      description="The schedule's item name: Hinge(s), Closer(s), Mortise Cylinder(s)")
    door_width: str | float | None = Field(None, alias="doorWidth",
                                           description="Door width (36, 36 in, 3'-0), for a plate "
                                                       "sized less door width (LDW)")
    quantity: float = Field(1, gt=0)


class PriceRequest(BaseModel):
    items: list[PriceItem] = Field(max_length=_MAX_ITEMS)


def _price_all(items: list[PriceItem]) -> list[dict[str, Any]]:
    books = store.mapped_books()
    codes = store.maker_codes()
    written = [makers.Written(i.id, i.manufacturer, i.model_number, i.finish, i.size, i.description,
                              i.name) for i in items]
    maker_of = {n: manufacturer_of(w.manufacturer, set(books), codes) for n, w in enumerate(written)}
    stored: dict[str, dict[str, dict]] = {}
    for maker in {m for m in maker_of.values() if m}:
        keys = ["|".join(w.product()) for n, w in enumerate(written) if maker_of[n] == maker]
        stored[maker] = store.ai_prices_for(maker, sorted(set(keys)))
    out = []
    for n, (item, w) in enumerate(zip(items, written)):
        maker = maker_of[n]
        if maker is None:
            q = {"status": "book_not_loaded", "ai_price_id": None, "manufacturer": item.manufacturer,
                 "list_price": None,
                 "extended_price": None, "parts": [], "options": [], "book": "", "effective_date": None,
                 "model": item.model_number or item.description, "finish": item.finish, "size": item.size,
                 "reasons": [f"No price book for '{item.manufacturer}' has been loaded"]}
        else:
            q = ai_pricing.as_quote(maker, w, item.quantity, stored[maker].get("|".join(w.product())))
        out.append({"id": item.id, **q})
    return out


@router.post("/prices")
async def price_items(body: PriceRequest) -> dict[str, Any]:
    """List price for each item, from the prices read by AI.

    The same answer shape as before pricing moved to AI: `priced` is a price a
    person confirmed, `needs_review` one two models agreed on and nobody has
    confirmed yet (or one the AI could not settle), `not_found` a product not
    priced with AI yet -- run POST /price-books/ai-price for it.
    """
    try:
        priced = await asyncio.to_thread(_price_all, body.items)
    except NoDatabase as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"No database configured: {exc}") from exc
    counts: dict[str, int] = {}
    for p in priced:
        counts[p["status"]] = counts.get(p["status"], 0) + 1
    return {"items": priced, "counts": counts}


@router.get("/price-books")
async def price_books() -> dict[str, Any]:
    """The books loaded, and how much of each was read.

    `manufacturers` is each maker once, by full name, for a dropdown: a maker
    with several books (dormakaba has nine) appears once there and once per
    book in `books`.
    """
    try:
        mapped = await asyncio.to_thread(store.mapped_books)
    except NoDatabase as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"No database configured: {exc}") from exc
    # From the book maps, as the AI reads them. The fields the code-read
    # prices filled (rows read, pages unread) no longer exist and are null.
    books = [{"manufacturer": maker, "file_name": name, "effective_date": None, "page_count": None,
              "reader": "ai", "rows_read": None, "pages_unread": None, "prices_unread": None}
             for maker, names in mapped.items() for name in names]
    return {"manufacturers": sorted(mapped, key=str.lower),
            "books": sorted(books, key=lambda b: (b["manufacturer"].lower(), b["file_name"]))}


def _search(manufacturer: str, q: str, limit: int) -> dict[str, Any]:
    maker = manufacturer_of(manufacturer, set(store.mapped_books()), store.maker_codes())
    if maker is None:
        raise LookupError(manufacturer)
    query = search.read_query(q)
    if len(query.key) < search.MIN_KEY:
        return {"manufacturer": maker, "query": {"model": query.key, "finish": query.finish,
                                                 "size": list(query.size) if query.size else None},
                "results": [], "more": False}
    found = [ai_pricing.as_suggestion(r) for r in store.settled_ai_prices(maker, query.key, limit + 1)]
    return {
        "manufacturer": maker,
        "query": {"model": query.key, "finish": query.finish,
                  "size": list(query.size) if query.size else None},
        "results": found[:limit],
        "more": len(found) > limit,
    }


@router.get("/price-books/search")
async def search_price_books(
    manufacturer: str = Query(..., description="Full name (Hager) or schedule code (HA)"),
    q: str = Query(..., max_length=100, description="Model as typed, optionally with finish and size"),
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """Products whose model starts with what was typed, for suggestions as one types.

    Fewer than two letters or digits of model returns no results rather than
    the whole book. `more` says the list was cut at `limit`.
    """
    try:
        return await asyncio.to_thread(_search, manufacturer, q, limit)
    except LookupError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"No price book for '{manufacturer}' has been loaded")
    except NoDatabase as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"No database configured: {exc}") from exc


# --------------------------------------------------------------------------- #
# which maker each item is, and the code table behind it
# --------------------------------------------------------------------------- #

class IdentifyRequest(BaseModel):
    items: list[PriceItem] = Field(max_length=_MAX_ITEMS)
    ai: bool = Field(True, description="Ask the AI about codes the table does not know")


def _resolve_all(groups: dict[str, makers.Group]) -> tuple[dict[str, makers.Resolved], dict[str, list[str]]]:
    books = store.mapped_books()
    codes = store.maker_codes()
    out = {}
    for key, g in groups.items():
        maker, found_by, verified = makers.resolve(g.written, set(books), codes)
        out[key] = makers.Resolved(g.written, maker, found_by, verified, sorted(books.get(maker, [])))
    return out, books


def _check(resolved: makers.Resolved, g: makers.Group) -> None:
    """How many of the group's products the maker's book maps list on a page."""
    if not resolved.books:
        return
    wanted = {c for i in g.items for c in i.candidates()}
    try:
        printed = store.maps_print(resolved.books, sorted(wanted))
    except Exception as exc:  # noqa: BLE001 - no maps yet: the check is skipped, not failed
        log.warning("book maps not read: %s", exc)
        return
    resolved.checked = sum(1 for i in g.items if set(i.candidates()) & printed)


def _printed_by(groups: dict[str, makers.Group], resolved: dict[str, makers.Resolved],
                books: dict[str, list[str]]) -> dict[str, list[str]]:
    """For the unknown codes' models, the makers whose book maps print them."""
    wanted = sorted({c for k, r in resolved.items() if r.found_by == "none" and k
                     for i in groups[k].items for c in i.candidates()})
    try:
        found = store.who_prints(wanted)
    except Exception as exc:  # noqa: BLE001 - no maps yet: the AI is asked without them
        log.warning("book maps not read: %s", exc)
        return {}
    maker_of = {f: m for m, files in books.items() for f in files}
    return {model: sorted({maker_of[f] for f in files if f in maker_of})
            for model, files in found.items()}


@router.post("/price-books/identify")
async def identify_makers(body: IdentifyRequest) -> dict[str, Any]:
    """Which manufacturer, and which books, each item's maker code means.

    Items are grouped by the maker as written, and each group says how many
    distinct products it holds (quantity is never counted), so the pricing
    can be batched one maker's books at a time. A code the table does not know
    is asked of the AI when `ai` is true; its answer is stored unverified.
    """
    items = [makers.Written(i.id, i.manufacturer, i.model_number, i.finish, i.size,
                            i.description, i.name, measure.door_width(i.door_width)) for i in body.items]
    groups = makers.group(items)
    try:
        resolved, books = await asyncio.to_thread(_resolve_all, groups)
    except NoDatabase as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"No database configured: {exc}") from exc

    ai_used: dict[str, Any] | None = None
    unknown = {groups[k].written: [i.sample() for i in groups[k].items][:8]
               for k, r in resolved.items() if r.found_by == "none" and k}
    if body.ai and unknown:
        model = get_settings().ai_price
        printed = await asyncio.to_thread(_printed_by, groups, resolved, books)
        calls = call_log.start()
        try:
            answer = await makers.ask_ai(unknown, sorted(books, key=str.lower), model, printed)
        except AiUnavailableError:
            answer = None
        if calls:
            try:
                await asyncio.to_thread(store.save_calls, calls, None, None)
            except Exception:  # noqa: BLE001 - a lost log line must not fail the answer
                log.exception("maker-code calls not stored")
        if answer is not None:
            ai_used = {"model": answer.model, "codes_asked": len(unknown),
                       "prompt_tokens": answer.prompt_tokens,
                       "completion_tokens": answer.completion_tokens, "usd": round(answer.usd, 6)}
            for key, r in resolved.items():
                maker, why = answer.picks.get(r.written, (None, ""))
                if maker is None:
                    continue
                r.manufacturer, r.found_by, r.verified = maker, "ai", False
                r.books = sorted(books.get(maker, []))
                await asyncio.to_thread(store.set_code, makers.Code(key, maker, "ai", False, why[:200]))

    await asyncio.gather(*(asyncio.to_thread(_check, r, groups[k])
                           for k, r in resolved.items() if r.manufacturer))

    by_maker: dict[str, dict[str, Any]] = {}
    for k, r in resolved.items():
        if not r.manufacturer:
            continue
        m = by_maker.setdefault(r.manufacturer, {
            "manufacturer": r.manufacturer, "books": r.books, "written_as": [],
            "items": 0, "products": set()})
        m["written_as"].append(r.written)
        m["items"] += len(groups[k].items)
        m["products"] |= {i.product() for i in groups[k].items}
    return {
        "items": [{"id": i.id, "written": i.manufacturer,
                   "manufacturer": resolved[makers.match_key(i.manufacturer)].manufacturer}
                  for i in items],
        "makers": sorted(({**m, "products": len(m["products"])} for m in by_maker.values()),
                         key=lambda m: -m["items"]),
        "codes": [{"written": r.written, "manufacturer": r.manufacturer, "found_by": r.found_by,
                   "verified": r.verified, "books": r.books, "items": len(groups[k].items),
                   "products": groups[k].products, "printed_in_maps": r.checked}
                  for k, r in resolved.items()],
        "unidentified": [{"written": r.written, "items": len(groups[k].items)}
                         for k, r in resolved.items() if not r.manufacturer],
        "ai": ai_used,
    }


# --------------------------------------------------------------------------- #
# step A: which pages price each product
# --------------------------------------------------------------------------- #

class FindPagesRequest(BaseModel):
    items: list[PriceItem] = Field(max_length=_MAX_ITEMS)


async def _step_a(maker: str, files: list[str], items: list[makers.Written], model: str) -> dict[str, Any]:
    books = await asyncio.to_thread(store.book_maps, files)
    if not books:
        return {"manufacturer": maker, "error": "These books have not been mapped yet",
                "products": []}
    try:
        found = await page_finder.find_pages(maker, books, items, model)
    except page_finder.CutOff as exc:
        return {"manufacturer": maker, "error": str(exc), "products": []}
    return {
        "manufacturer": maker,
        "products": [{"product": f.product.text(), "item_ids": f.product.item_ids,
                      "pages": [asdict(p) for p in f.pages], "dropped": f.dropped}
                     for f in found.found],
        "ai": {"model": found.model, "calls": found.calls, "prompt_tokens": found.prompt_tokens,
               "completion_tokens": found.completion_tokens, "usd": round(found.usd, 6)},
    }


@router.post("/price-books/find-pages")
async def find_pages(body: FindPagesRequest) -> dict[str, Any]:
    """Step A of pricing with AI: the pages that price each distinct product.

    Items are grouped by maker (from the code table only; run `identify` first
    for unknown codes) and each maker's products go to the AI in one call with
    that maker's book maps. Every page named is checked to be a real price page
    of the maker's books; `model_on_page` says whether the map lists the
    product's model there. No price is read here -- that is step B.
    """
    items = [makers.Written(i.id, i.manufacturer, i.model_number, i.finish, i.size,
                            i.description, i.name, measure.door_width(i.door_width)) for i in body.items]
    groups = makers.group(items)
    try:
        resolved, books = await asyncio.to_thread(_resolve_all, groups)
    except NoDatabase as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"No database configured: {exc}") from exc
    per_maker: dict[str, list[makers.Written]] = {}
    for k, r in resolved.items():
        if r.manufacturer and r.books:
            per_maker.setdefault(r.manufacturer, []).extend(groups[k].items)
    model = get_settings().ai_price
    try:
        answers = await asyncio.gather(*(_step_a(m, books[m], its, model)
                                         for m, its in per_maker.items()))
    except AiUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"AI not configured: {exc}") from exc
    return {
        "makers": list(answers),
        "not_looked_up": [{"written": r.written, "manufacturer": r.manufacturer,
                           "items": len(groups[k].items)}
                          for k, r in resolved.items() if not (r.manufacturer and r.books)],
        "usd": round(sum(a.get("ai", {}).get("usd", 0) for a in answers), 6),
    }


# --------------------------------------------------------------------------- #
# pricing a project with AI, and a person confirming what it read
# --------------------------------------------------------------------------- #

class AiPriceRequest(BaseModel):
    items: list[PriceItem] = Field(max_length=_MAX_ITEMS)
    project_id: str | None = Field(None, max_length=100)
    reuse_verified: bool = Field(True, description="Take a product a person has verified from the "
                                                   "table instead of reading it again")
    engine: Literal["pipeline", "agent"] | None = Field(
        None, description="pipeline: step A finds pages, step B reads them. agent: an agent searches "
                          "the whole book and builds the price part by part, a second model checks "
                          "it. Empty takes the server's setting (AI_PRICE_ENGINE).")


# Runs in progress, held so the event loop does not drop them half way.
_RUNS: set[asyncio.Task] = set()


async def _run(run_id: str, items: list[makers.Written], reuse: bool,
               project_id: str | None, engine: str) -> None:
    settings = get_settings()
    calls = call_log.start()
    price_project = agent_pricing.price_project if engine == "agent" else ai_pricing.price_project
    try:
        result = await price_project(items, run_id, settings.ai_price, settings.ai_price_check_model, reuse)
    except Exception as exc:  # noqa: BLE001 - a failed run is reported on the run
        log.exception("AI price run %s failed", run_id)
        await asyncio.to_thread(store.finish_run, run_id, None, f"{type(exc).__name__}: {exc}")
    else:
        await asyncio.to_thread(store.finish_run, run_id, result)
    finally:
        # Paid for whether the run finished or not.
        try:
            await asyncio.to_thread(store.save_calls, calls, run_id, project_id)
        except Exception:  # noqa: BLE001 - a lost log line must not fail the run
            log.exception("AI price run %s: calls not stored", run_id)


@router.post("/price-books/ai-price", status_code=status.HTTP_202_ACCEPTED)
async def start_ai_price(body: AiPriceRequest) -> dict[str, Any]:
    """Price items with AI, in the background. Poll GET /price-books/ai-price/{run_id}.

    Every distinct product is read by two models from the pages of its maker's
    books, checked, and stored with its verdict. Quantity is ignored.
    """
    if not get_settings().ai_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="AI not configured: OPENROUTER_API_KEY is not set")
    items = [makers.Written(i.id or f"#{n}", i.manufacturer, i.model_number, i.finish, i.size,
                            i.description, i.name, measure.door_width(i.door_width)) for n, i in enumerate(body.items)]
    engine = body.engine or get_settings().ai_price_engine
    try:
        run_id = await asyncio.to_thread(
            store.start_run, [asdict(i) for i in items], body.project_id)
        if engine == "agent":
            await asyncio.to_thread(store.queue_items, run_id, [i.id for i in items])
    except NoDatabase as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"No database configured: {exc}") from exc
    task = asyncio.create_task(_run(run_id, items, body.reuse_verified, body.project_id, engine))
    _RUNS.add(task)
    task.add_done_callback(_RUNS.discard)
    return {"run_id": run_id, "status": "running", "items": len(items), "engine": engine}


@router.get("/price-books/ai-price/{run_id}")
async def ai_price_run(run_id: str) -> dict[str, Any]:
    """A run's status; once done, each item's answer and what the run cost.

    An agent run also gives `progress`: every item as it stands now -- queued,
    working with the step its agent is on, or done with its answer -- so rows
    can be shown as they finish instead of when the run ends.
    """
    try:
        run = await asyncio.to_thread(store.run_of, run_id)
        progress = await asyncio.to_thread(store.run_items, run_id) if run else []
    except NoDatabase as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"No database configured: {exc}") from exc
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such run")
    done = sum(1 for p in progress if p["status"] == "done")
    return {**{k: run[k] for k in ("id", "project_id", "status", "result", "counts", "usd", "error",
                                   "created_at", "finished_at")},
            "progress": progress, "done": done, "total": len(progress)}


class ConfirmBody(BaseModel):
    list_price: float | None = Field(None, gt=0, description="The price as the person read it; "
                                                             "empty takes the agreed price")
    note: str = Field("", max_length=300)


@router.post("/price-books/ai-prices/{price_id}/confirm")
async def confirm_ai_price(price_id: str, body: ConfirmBody) -> dict[str, Any]:
    """A person settles an AI price: it becomes verified and is reused from then on.

    With no `list_price`, the agreed price is confirmed as it is. A price that
    was not agreed needs the person's `list_price` -- one of the answers shown,
    or what they read off the page themselves.
    """
    try:
        row = await asyncio.to_thread(store.ai_price, price_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such AI price")
        price = body.list_price if body.list_price is not None else row["list_price"]
        if price is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="This price was not agreed; send the list_price to confirm")
        return await asyncio.to_thread(store.confirm_ai_price, price_id, float(price), body.note,
                                       ai_pricing.parts_for(row, float(price)))
    except NoDatabase as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"No database configured: {exc}") from exc


@router.get("/price-books/ai-prices")
async def list_ai_prices(
    manufacturer: str = Query(..., description="Full name (Hager) or schedule code (HA)"),
    q: str = Query("", max_length=100, description="The start of the model"),
    verified: bool = Query(True, description="Only prices a person has confirmed"),
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """AI prices of a maker's products, for suggestions on a new project."""
    def _find():
        maker = manufacturer_of(manufacturer, set(store.mapped_books()), store.maker_codes())
        if maker is None:
            raise LookupError(manufacturer)
        return maker, store.search_ai_prices(maker, makers.match_key(q), verified, limit)

    try:
        maker, rows = await asyncio.to_thread(_find)
    except LookupError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"No price book for '{manufacturer}' has been loaded")
    except NoDatabase as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"No database configured: {exc}") from exc
    return {"manufacturer": maker, "results": rows}


class CodeBody(BaseModel):
    manufacturer: str = Field(..., min_length=1, max_length=100)
    note: str = Field("", max_length=200)
    no_book: bool = Field(False, description="The maker has no price book: the code then answers "
                                             "'no price book for this maker' instead of being matched "
                                             "to another maker's book")


@router.get("/price-books/manufacturer-codes")
async def list_codes() -> dict[str, Any]:
    """Every maker code the lookup knows, and whether a person has confirmed it."""
    try:
        codes = await asyncio.to_thread(store.maker_codes)
    except NoDatabase as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"No database configured: {exc}") from exc
    return {"codes": [asdict(c) for c in sorted(codes.values(), key=lambda c: c.code)]}


@router.put("/price-books/manufacturer-codes/{code}")
async def put_code(code: str, body: CodeBody) -> dict[str, Any]:
    """Add or correct a code, as a person: it is stored verified.

    The maker must be one whose book is loaded, by its full name or a known
    code, so a typo cannot point a code at no book. A maker with no book is
    saved only as the explicit choice `no_book: true` ("AK" is Alarm Controls,
    whose prices are not on file): the code then answers "no price book" rather
    than being guessed at or matched to another maker's book. `no_book` for a
    maker whose book is loaded is refused -- that is a typo or a mistake.
    """
    key = makers.match_key(code)
    if not key:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="A code needs at least one letter or digit")

    def _put() -> makers.Code:
        loaded, codes = set(store.mapped_books()), store.maker_codes()
        maker = manufacturer_of(body.manufacturer, loaded, codes)
        if body.no_book:
            if maker is not None:
                raise ValueError(maker)
            # A known code typed as the name ("HS") saves the maker it stands for.
            named, _, _ = makers.resolve(body.manufacturer, set(), codes)
            entry = makers.Code(key, named or " ".join(body.manufacturer.split()), "user", True,
                                body.note, no_book=True)
        elif maker is None:
            raise LookupError(body.manufacturer)
        else:
            entry = makers.Code(key, maker, "user", True, body.note)
        store.set_code(entry)
        return entry

    try:
        entry = await asyncio.to_thread(_put)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"{exc} has a price book loaded; save the code without no_book")
    except LookupError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"No price book for '{body.manufacturer}' has been loaded. If this "
                                   "maker has no price book, save the code with no_book: true")
    except NoDatabase as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"No database configured: {exc}") from exc
    return asdict(entry)


@router.delete("/price-books/manufacturer-codes/{code}")
async def delete_code(code: str) -> dict[str, Any]:
    """Remove a code saved by mistake.

    A standard code (HA, VD...) removed from the table still reads as the
    standard one: `now` says what the code means after the delete, null when
    it is unknown again.
    """
    key = makers.match_key(code)
    try:
        deleted = await asyncio.to_thread(store.delete_code, key) if key else False
        now = (await asyncio.to_thread(store.maker_codes)).get(key) if deleted else None
    except NoDatabase as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"No database configured: {exc}") from exc
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No saved code '{code}'")
    return {"deleted": key, "now": asdict(now) if now else None}
