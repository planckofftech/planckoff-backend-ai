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
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import require_api_key
from app.db.client import NoDatabase
from app.price_book import search, store
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
    quantity: float = Field(1, gt=0)


class PriceRequest(BaseModel):
    items: list[PriceItem] = Field(max_length=_MAX_ITEMS)


def _price_all(items: list[PriceItem]) -> list[dict[str, Any]]:
    source = store.DbSource()
    out = []
    for item in items:
        q = quote(Item(item.manufacturer, item.model_number, item.finish, item.size,
                       item.description, item.quantity), source)
        out.append({"id": item.id, **asdict(q)})
    return out


@router.post("/prices")
async def price_items(body: PriceRequest) -> dict[str, Any]:
    """List price for each item, with its parts and the pages they were read from."""
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
        books = await asyncio.to_thread(store.loaded_books)
    except NoDatabase as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"No database configured: {exc}") from exc
    makers = sorted({b["manufacturer"] for b in books}, key=str.lower)
    return {"manufacturers": makers, "books": books}


def _search(manufacturer: str, q: str, limit: int) -> dict[str, Any]:
    source = store.DbSource()
    loaded = {b.manufacturer for b in source.books().values()}
    maker = manufacturer_of(manufacturer, loaded)
    if maker is None:
        raise LookupError(manufacturer)
    query = search.read_query(q)
    found = search.suggest(source, maker, q, limit + 1)
    books = source.books()
    return {
        "manufacturer": maker,
        "query": {"model": query.key, "finish": query.finish,
                  "size": list(query.size) if query.size else None},
        "results": [{**asdict(s), "effective_date": books[s.book].effective_date
                     if s.book in books else None} for s in found[:limit]],
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
