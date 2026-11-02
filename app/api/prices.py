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

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import require_api_key
from app.db.client import NoDatabase
from app.price_book import store
from app.price_book.lookup import Item, quote

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
    """The books loaded, and how much of each was read."""
    try:
        books = await asyncio.to_thread(store.loaded_books)
    except NoDatabase as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"No database configured: {exc}") from exc
    return {"books": books}
