"""Watching the makers' websites for new price books: the sources, the runs, what was found.

    GET    /price-books/sources                 where each maker's book is looked for
    POST   /price-books/sources                 add one (a maker and URL already listed is changed)
    PUT    /price-books/sources/{id}            change one
    DELETE /price-books/sources/{id}
    POST   /price-books/watch                   check now, in the background (a scheduler calls this)
    GET    /price-books/watch/runs              the latest runs
    GET    /price-books/watch/runs/{run_id}     one run and what each source answered
    GET    /price-books/updates                 books found that are not loaded (?status=new)
    GET    /price-books/updates/{id}/file       a link to download the found PDF
    POST   /price-books/updates/{id}/decide     accepted or ignored, with a note

A found book is never loaded by itself; see app/price_book/watch.py for why.
Accepting one records the decision -- loading it is still mapping it with
scripts/map_price_books.py and listing it in app/price_book/readers.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from app.api.deps import require_api_key
from app.db import files
from app.db.client import NoDatabase
from app.price_book import store, watch

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["price watch"], dependencies=[Depends(require_api_key)])

# Runs in progress, held so the event loop does not drop them half way. One at
# a time in this process: two runs would ask the same sites twice.
_RUNS: set[asyncio.Task] = set()


def _no_db(exc: NoDatabase) -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                         detail=f"No database configured: {exc}")


class SourceBody(BaseModel):
    manufacturer: str = Field(min_length=1, max_length=100)
    url: str = Field("", max_length=2000)
    kind: Literal["pdf", "page", "index", "portal", "rep", "email"]
    link_pattern: str = Field("", max_length=300,
                              description="Picks the price-book links on a page; empty uses the default")
    active: bool = True
    verified: bool = False
    note: str = Field("", max_length=1000)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip()
        if v and not re.match(r"https?://", v, re.IGNORECASE):
            raise ValueError("a URL starts with http:// or https://")
        return v

    @field_validator("link_pattern")
    @classmethod
    def _pattern(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError(f"not a pattern: {exc}") from exc
        return v

    def checked(self) -> "SourceBody":
        if self.kind in watch.FETCHED and not self.url:
            raise HTTPException(status_code=422,
                                detail=f"A '{self.kind}' source needs its URL")
        return self


@router.get("/price-books/sources")
async def list_sources() -> dict[str, Any]:
    try:
        rows = await asyncio.to_thread(store.watch_sources)
    except NoDatabase as exc:
        raise _no_db(exc) from exc
    # The per-link state is the watcher's own bookkeeping, not for a screen.
    for r in rows:
        r["links_watched"] = len((r.pop("state", None) or {}).get("links") or {})
    return {"sources": rows}


@router.post("/price-books/sources", status_code=status.HTTP_201_CREATED)
async def add_source(body: SourceBody) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(store.save_source, body.checked().model_dump())
    except NoDatabase as exc:
        raise _no_db(exc) from exc


@router.put("/price-books/sources/{source_id}")
async def change_source(source_id: str, body: SourceBody) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(store.save_source, body.checked().model_dump(), source_id)
    except NoDatabase as exc:
        raise _no_db(exc) from exc
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such source") from None


@router.delete("/price-books/sources/{source_id}")
async def remove_source(source_id: str) -> dict[str, Any]:
    try:
        gone = await asyncio.to_thread(store.delete_source, source_id)
    except NoDatabase as exc:
        raise _no_db(exc) from exc
    if not gone:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such source")
    return {"deleted": source_id}


class WatchBody(BaseModel):
    source_ids: list[str] | None = Field(None, description="Check only these; empty checks every active source")


async def _run(run_id: str, source_ids: list[str] | None) -> None:
    try:
        summary = await asyncio.to_thread(watch.run, run_id, source_ids)
    except Exception as exc:  # noqa: BLE001 - a failed run is reported on the run
        log.exception("price watch run %s failed", run_id)
        await asyncio.to_thread(store.finish_watch_run, run_id, None, f"{type(exc).__name__}: {exc}")
    else:
        await asyncio.to_thread(store.finish_watch_run, run_id, summary)


@router.post("/price-books/watch", status_code=status.HTTP_202_ACCEPTED)
async def start_watch(body: WatchBody | None = None) -> dict[str, Any]:
    """Check the sources now, in the background. Poll GET /price-books/watch/runs/{run_id}."""
    if _RUNS:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A check is already running")
    try:
        run_id = await asyncio.to_thread(store.start_watch_run)
    except NoDatabase as exc:
        raise _no_db(exc) from exc
    task = asyncio.create_task(_run(run_id, body.source_ids if body else None))
    _RUNS.add(task)
    task.add_done_callback(_RUNS.discard)
    return {"run_id": run_id, "status": "running"}


@router.get("/price-books/watch/runs")
async def list_watch_runs(limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    try:
        return {"runs": await asyncio.to_thread(store.watch_runs, limit)}
    except NoDatabase as exc:
        raise _no_db(exc) from exc


@router.get("/price-books/watch/runs/{run_id}")
async def watch_run(run_id: str) -> dict[str, Any]:
    try:
        run = await asyncio.to_thread(store.watch_run, run_id)
    except NoDatabase as exc:
        raise _no_db(exc) from exc
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such run")
    run["status"] = "failed" if run["error"] else ("done" if run["finished_at"] else "running")
    return run


@router.get("/price-books/updates")
async def list_updates(status_: Literal["new", "accepted", "ignored"] | None = Query(None, alias="status"),
                       limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    try:
        return {"updates": await asyncio.to_thread(store.updates, status_, limit)}
    except NoDatabase as exc:
        raise _no_db(exc) from exc


@router.get("/price-books/updates/{update_id}/file")
async def update_file(update_id: str) -> dict[str, Any]:
    """A short-lived link to the PDF as it was found, or the maker's own link when it was not kept."""
    try:
        found = await asyncio.to_thread(store.update_of, update_id)
    except NoDatabase as exc:
        raise _no_db(exc) from exc
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such update")
    if found["staged_key"] and files.available():
        return {"url": await asyncio.to_thread(files.read_url, found["staged_key"]), "kept": True}
    return {"url": found["url"], "kept": False}


class DecideBody(BaseModel):
    status: Literal["accepted", "ignored"]
    note: str = Field("", max_length=1000)


@router.post("/price-books/updates/{update_id}/decide")
async def decide_update(update_id: str, body: DecideBody) -> dict[str, Any]:
    try:
        got = await asyncio.to_thread(store.decide_update, update_id, body.status, body.note)
    except NoDatabase as exc:
        raise _no_db(exc) from exc
    if got is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such update")
    return got
