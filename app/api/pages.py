"""Remove a plan page from a drawing set, and put it back.

    DELETE /documents/{id}/pages/{page}            remove the page
    DELETE /documents/{id}/removed-pages/{page}    put that page back
    DELETE /documents/{id}/removed-pages           put every removed page back

GET /documents/{id} lists the removed pages under `removed_pages`.

Removing a page takes its boxes off and nothing else: no door and no schedule
row changes. A door whose only box was there is left unplaced. Plan reads and
area reads do not look at the page while it is removed. Putting it back
returns its boxes as they were. See app/db/removed_pages.py.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, status

from app.api import storage
from app.api.deps import Caller, require_caller
from app.db import removed_pages, store

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["pages"])

_NOT_SET_UP = "Removing pages is not set up on this server yet."
# Putting a page back while a plan read runs would have that read, which
# skipped the page, store the set without it again.
_BUSY = ("A plan read is running on this set. Put the page back when it has "
         "finished.")


def _document(db, document_id: str) -> dict:
    found = (db.table("documents").select("id, page_count, plans_uri")
             .eq("id", document_id).execute().data)
    if not found:
        raise HTTPException(status_code=404, detail="No such document.")
    return found[0]


@router.delete("/documents/{document_id}/pages/{page}")
async def remove_page(document_id: str, page: int = Path(ge=1),
                      caller: Caller = Depends(require_caller)):
    """Remove a plan page from the set.

    Its boxes come off -- placed by a read or by a person -- and are kept for
    undo. No door or schedule row changes; `doors_unplaced` names the doors
    whose only box was on the page. `counted_elsewhere` names the doors that
    were counted on this page and are now counted on another sheet they are
    drawn on. Removing a page already removed changes nothing
    (`already_removed`).
    """
    db = storage._db()
    doc = _document(db, document_id)
    # The page count is the schedule file's; on a set whose drawings came as
    # a second file it says nothing about the drawings.
    count = doc.get("page_count")
    if not doc.get("plans_uri") and count and page > count:
        raise HTTPException(status_code=422,
                            detail=f"This set has {count} pages; there is no page {page}.")
    if not removed_pages.available():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=_NOT_SET_UP)
    return removed_pages.remove(document_id, page, created_by=caller.user_id)


@router.delete("/documents/{document_id}/removed-pages/{page}")
async def restore_page(document_id: str, page: int = Path(ge=1),
                       caller: Caller = Depends(require_caller)):
    """Put a removed page back, with its boxes as they were. Where a box has
    since been placed for the same door on that page, the newer one stands."""
    db = storage._db()
    _document(db, document_id)
    if store.plan_running(document_id):
        raise HTTPException(status_code=409, detail=_BUSY)
    done = removed_pages.restore(document_id, page)
    if done is None:
        raise HTTPException(status_code=404,
                            detail="That page was not removed from this set.")
    return {"document_id": document_id, "restored": done}


@router.delete("/documents/{document_id}/removed-pages")
async def restore_all_pages(document_id: str,
                            caller: Caller = Depends(require_caller)):
    """Put every removed page back. Nothing removed: `restored` is empty."""
    db = storage._db()
    _document(db, document_id)
    if store.plan_running(document_id):
        raise HTTPException(status_code=409, detail=_BUSY)
    return {"document_id": document_id,
            "restored": removed_pages.restore(document_id) or []}
