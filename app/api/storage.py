"""Projects, stored documents and corrections.

Everything here writes, or reads what was written. The takeoff itself is done by
`routes.py`; this is the memory of it.

Writes are backend-only by design. `doors` and `detections` are a record of what
was read off a drawing, and a record a browser can edit is not worth keeping --
so nothing here accepts a door from a caller. The one thing a person *may*
change goes to `corrections`, alongside the original rather than over it.
"""

from __future__ import annotations

import logging

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field

from app.api.deps import require_api_key
from app.api.offload import in_worker
from app.core.pdf_doc import NotAPdfError
from app.db import store
from app.db.client import NoDatabase, client
from app.pipeline import NoRowsError, NoScheduleFoundError, extract
from app.plan_pipeline import audit

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["storage"])


def _db():
    try:
        return client()
    except NoDatabase as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No database configured: {exc}",
        ) from exc


class NewProject(BaseModel):
    org: str = Field(description="Organisation name; created if new")
    name: str = Field(description="Job name, e.g. 'BMK Pharma'")
    code: str | None = Field(None, description="The firm's own job number")


class Correction(BaseModel):
    """One field of one door, changed by a person.

    Keyed on the door number rather than a row, so it survives the document
    being read again -- which happens every time the extractor improves. A
    correction tied to a row would be destroyed by the very improvement it was
    compensating for.
    """

    door_tag: str
    field: str = Field(description="Canonical field, e.g. 'door_width'")
    value: str = Field(description="What it should be")
    note: str | None = None


@router.post("/projects", status_code=status.HTTP_201_CREATED)
async def create_project(body: NewProject, _key: str = Depends(require_api_key)):
    """Create a job, or return the one already there under that name."""
    _db()
    org_id = store.ensure_org(body.org)
    project_id = store.ensure_project(org_id, body.name, body.code)
    return {"id": project_id, "org_id": org_id, "name": body.name}


@router.get("/projects")
async def list_projects(_key: str = Depends(require_api_key)):
    """Every job, with what is in it -- read from `project_summary`.

    The view, not the tables. It is the shape the frontend is promised, so the
    tables underneath stay free to change.
    """
    return _db().table("project_summary").select("*").execute().data


@router.post("/projects/{project_id}/documents",
             status_code=status.HTTP_201_CREATED)
async def add_document(
    project_id: str,
    file: UploadFile = File(..., description="Construction PDF (multipart)"),
    revision: str | None = Query(None, description="'IFC', 'Addendum 1', ..."),
    plans: bool = Query(True, description="Also locate the doors on the plans"),
    allow_ai: bool = Query(True, description="Permit the vision fallback tier"),
    reuse: bool = Query(True, description="Return a stored reading if this "
                                          "exact file is already in this job"),
    _key: str = Depends(require_api_key),
):
    """Upload a drawing set, read it, and remember the result.

    The one call the frontend needs to add work to a job. `reuse` is why it is
    worth having: the same set is re-uploaded constantly under new names, and
    recognising it by its bytes turns eighty seconds and a possible AI bill into
    a lookup. Pass `reuse=false` to force a re-read after the extractor changes.
    """
    from app.api.routes import _spooled_upload

    _db()
    async with _spooled_upload(file) as path:
        sha256, size_bytes = store.digest_path(path)

        if reuse:
            found = store.find_document(project_id, sha256)
            if found:
                log.info("db: %s is already in this project; not re-reading",
                         file.filename)
                return {"document_id": found["id"], "reused": True,
                        "filename": found["filename"]}

        try:
            result = await in_worker(extract(path, allow_ai=allow_ai))
        except (NotAPdfError, NoScheduleFoundError, NoRowsError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        document_id = store.save_extraction(
            project_id=project_id, filename=file.filename or "upload.pdf",
            result=result, sha256=sha256, size_bytes=size_bytes,
            revision=revision)
        if not document_id:
            raise HTTPException(status_code=503,
                                detail="Read the schedule but could not store "
                                       "it. The result was not saved.")

        # Locating the doors on the drawings is a second, slower pass, and it
        # can fail on its own without costing the schedule that was just read.
        located = None
        if plans:
            try:
                found = await in_worker(audit(
                    path, rows=result.rows,
                    schedule_page=result.source_pages[0]
                    if result.source_pages else None))
                store.save_audit(document_id, found)
                located = len(found.located)
            except Exception as exc:  # noqa: BLE001 - the schedule still stands
                log.warning("db: schedule stored, plan audit failed: %s", exc)

        return {"document_id": document_id, "reused": False,
                "doors": result.row_count, "method": result.method.value,
                "located_on_plans": located}


@router.get("/projects/{project_id}/documents")
async def list_documents(project_id: str, _key: str = Depends(require_api_key)):
    """The drawing sets in a job, newest first."""
    return (_db().table("documents").select(
        "id, filename, revision, page_count, size_bytes, created_at")
        .eq("project_id", project_id)
        .order("created_at", desc=True).execute().data)


@router.get("/documents/{document_id}")
async def stored_document(
    document_id: str,
    detections: bool = Query(True, description="Include the measured doors"),
    _key: str = Depends(require_api_key),
):
    """A takeoff as stored -- no PDF, no re-processing.

    This is the whole point of the database. Re-reading BMK costs eighty
    seconds and, with the detector on, real money; reading it back costs a
    query. The schedule comes from `doors_current`, so any correction a person
    has made is already applied and flagged.
    """
    db = _db()
    found = db.table("documents").select("*").eq("id", document_id).execute()
    if not found.data:
        raise HTTPException(status_code=404, detail="No such document.")

    doors = (db.table("doors_current").select("*")
             .eq("document_id", document_id)
             .order("row_index").execute().data)
    sheets = (db.table("sheets").select("*")
              .eq("document_id", document_id).order("page").execute().data)
    out = {"document": found.data[0], "doors": doors, "sheets": sheets}
    if detections:
        out["detections"] = (db.table("detections").select("*")
                             .eq("document_id", document_id).execute().data)
    return out


@router.post("/documents/{document_id}/corrections",
             status_code=status.HTTP_201_CREATED)
async def correct(document_id: str, body: Correction,
                  _key: str = Depends(require_api_key)):
    """Record that a person changed a value.

    The extracted value is never overwritten. `was` and `now` both stay, because
    the question asked later is not what the number is -- it is who changed it,
    and when.
    """
    db = _db()
    found = db.table("documents").select("org_id").eq("id", document_id).execute()
    if not found.data:
        raise HTTPException(status_code=404, detail="No such document.")

    door = (db.table("doors").select(body.field)
            .eq("document_id", document_id).eq("door_tag", body.door_tag)
            .execute())
    if not door.data:
        raise HTTPException(
            status_code=404,
            detail=f"Door {body.door_tag} is not in this document.")

    row = {
        "org_id": found.data[0]["org_id"],
        "document_id": document_id,
        "door_tag": body.door_tag,
        "field": body.field,
        "was": door.data[0].get(body.field),
        "now": body.value,
        "note": body.note,
    }
    made = db.table("corrections").insert(row).execute()
    log.info("db: %s on door %s changed to %r",
             body.field, body.door_tag, body.value)
    return made.data[0]


@router.get("/documents/{document_id}/history")
async def history(document_id: str, _key: str = Depends(require_api_key)):
    """Every read of this document, and what each one found.

    The reason runs are logged at all: two rows here, with different
    `app_version`s and different `swings_measured`, is how a change is shown to
    have helped or hurt without anyone re-running six projects by hand.
    """
    return (_db().table("run_log").select("*")
            .eq("document_id", document_id)
            .order("created_at", desc=True).execute().data)
