"""Projects, stored documents and corrections.

Everything here writes, or reads what was written. The takeoff itself is done by
`routes.py`; this is the memory of it.

Writes are backend-only by design. `doors` and `detections` are a record of what
was read off a drawing, and a record a browser can edit is not worth keeping --
so nothing here accepts a door from a caller. The one thing a person *may*
change goes to `corrections`, alongside the original rather than over it.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

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
from app.config import get_settings
from app.api.offload import in_worker
from app.core.pdf_doc import NotAPdfError
from app.db import files, store
from app.db.client import NoDatabase, client
from app.pipeline import NoRowsError, NoScheduleFoundError, extract
from app.plan_pipeline import audit
from app.schemas import DoorRow

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
async def list_projects(
    archived: bool = Query(False, description="Show archived jobs instead of "
                                              "active ones"),
    _key: str = Depends(require_api_key),
):
    """Every job, with what is in it -- read from `project_summary`.

    The view, not the tables. It is the shape the frontend is promised, so the
    tables underneath stay free to change.

    Archived jobs are hidden rather than gone: pass `archived=true` to see them,
    which is also how anything archived by mistake is found again.
    """
    wanted = "archived" if archived else "active"
    return (_db().table("project_summary").select("*")
            .eq("status", wanted).execute().data)


class UploadRequest(BaseModel):
    filename: str
    sha256: str = Field(description="SHA-256 of the file, computed by the "
                                    "caller. It names the object, so the same "
                                    "set re-uploaded lands on the same key "
                                    "instead of filling the bucket.")


@router.post("/projects/{project_id}/uploads")
async def signed_upload(project_id: str, body: UploadRequest,
                        _key: str = Depends(require_api_key)):
    """A link the browser may PUT one file to, and the key it will live under.

    The file never passes through this service. That is what removes every
    request-size limit between the browser and storage -- a 115 MB drawing set
    goes straight to R2 -- and it is why this can run on a host that caps
    request bodies at 32 MB.

    The link is signed for one key and expires shortly, so the caller holds no
    credential and can reach nothing else. It also means the client never names
    a path, which is what would otherwise have to be defended against.
    """
    if not files.available():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No file store configured; upload the PDF directly instead.")
    _db()
    key = files.key_for(project_id, body.sha256)
    if files.exists(key):
        # Already here. Say so rather than issuing a link to overwrite it with
        # bytes that, by definition, are identical.
        return {"key": key, "upload_url": None, "already_stored": True}
    return {"key": key, "upload_url": files.upload_url(key),
            "already_stored": False,
            "expires_in": get_settings().upload_url_ttl}


class StoredUpload(BaseModel):
    key: str = Field(description="The key returned by /uploads, now uploaded")
    filename: str
    revision: str | None = None


@router.post("/projects/{project_id}/documents/from-storage",
             status_code=status.HTTP_201_CREATED)
async def add_uploaded_document(
    project_id: str,
    body: StoredUpload,
    plans: bool = Query(True, description="Also locate the doors on the plans"),
    allow_ai: bool = Query(True, description="Permit the vision fallback tier"),
    reuse: bool = Query(True, description="Return a stored reading if this "
                                          "exact file is already in this job"),
    _key: str = Depends(require_api_key),
):
    """Read a set the browser has already put in storage.

    The counterpart to `/uploads`: the bytes are in R2, this fetches them and
    does the work. The file stays there afterwards, because the plan viewer
    needs the original PDF to draw doors on a sheet long after this request has
    finished.
    """
    if not files.available():
        raise HTTPException(status_code=503,
                            detail="No file store configured.")
    _db()
    if not body.key.startswith(f"{project_id}/"):
        # The key was issued by `/uploads` for this project. One that is not
        # cannot have been, so it is either a mistake or an attempt to read
        # another job's drawings.
        raise HTTPException(status_code=400,
                            detail="That key does not belong to this project.")
    if not files.exists(body.key):
        raise HTTPException(status_code=404,
                            detail="Nothing has been uploaded under that key.")

    sha256 = Path(body.key).stem
    if reuse:
        found = store.find_document(project_id, sha256)
        if found:
            return {"document_id": found["id"], "reused": True,
                    "filename": found["filename"]}

    path = files.fetch(body.key)
    try:
        return await _read_and_store(
            project_id, path, filename=body.filename, sha256=sha256,
            size_bytes=files.size_of(body.key), revision=body.revision,
            source_uri=body.key, plans=plans, allow_ai=allow_ai)
    finally:
        files.discard(path)


async def _read_and_store(project_id: str, path, *, filename: str,
                          sha256: str, size_bytes: int, revision: str | None,
                          source_uri: str | None, plans: bool, allow_ai: bool):
    """Extract, store, then audit -- shared by both ways a file arrives."""
    try:
        result = await in_worker(extract(path, allow_ai=allow_ai))
    except (NotAPdfError, NoScheduleFoundError, NoRowsError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    document_id = store.save_extraction(
        project_id=project_id, filename=filename, result=result,
        sha256=sha256, size_bytes=size_bytes, revision=revision,
        source_uri=source_uri)
    if not document_id:
        raise HTTPException(status_code=503,
                            detail="Read the schedule but could not store it. "
                                   "The result was not saved.")

    # Locating the doors on the drawings is a second, slower pass, and it can
    # fail on its own without costing the schedule that was just read.
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

        # Keep the bytes if there is somewhere to keep them. The viewer needs
        # the original PDF later, and this route is the one where we hold it.
        source_uri = None
        if files.available():
            source_uri = files.key_for(project_id, sha256)
            try:
                files.put(source_uri, path)
            except Exception as exc:  # noqa: BLE001 - reading still works
                log.warning("r2: could not store %s: %s", file.filename, exc)
                source_uri = None

        return await _read_and_store(
            project_id, path, filename=file.filename or "upload.pdf",
            sha256=sha256, size_bytes=size_bytes, revision=revision,
            source_uri=source_uri, plans=plans, allow_ai=allow_ai)


@router.get("/projects/{project_id}/documents")
async def list_documents(
    project_id: str,
    archived: bool = Query(False, description="Show archived sets instead of "
                                              "active ones"),
    _key: str = Depends(require_api_key),
):
    """The drawing sets in a job, newest first.

    `source_uri` is null for a set whose file was never kept -- it cannot be
    re-rendered, so a viewer needs to know before it offers the option.
    """
    wanted = "archived" if archived else "active"
    return (_db().table("documents").select(
        "id, filename, revision, page_count, size_bytes, created_at, "
        "status, source_uri")
        .eq("project_id", project_id).eq("status", wanted)
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


@router.delete("/documents/{document_id}")
async def remove_document(
    document_id: str,
    hard: bool = Query(False, description="Destroy it instead of archiving. "
                                          "The doors, sheets, detections and "
                                          "corrections go too, and so does the "
                                          "stored PDF. There is no undo."),
    _key: str = Depends(require_api_key),
):
    """Archive a drawing set, or destroy it outright.

    Archiving by default is not timidity. Reading a set costs eighty seconds
    and, with the detector on, real money -- and a person who has since
    corrected forty doors by hand has put work into this row that no re-run
    brings back. Hiding it is reversible; deleting it is not.
    """
    db = _db()
    found = (db.table("documents").select("id, filename, source_uri")
             .eq("id", document_id).execute())
    if not found.data:
        raise HTTPException(status_code=404, detail="No such document.")
    doc = found.data[0]

    if not hard:
        store.set_document_status(document_id, "archived")
        return {"document_id": document_id, "archived": True,
                "filename": doc["filename"]}

    # Rows first. A file left behind is recoverable waste; a row pointing at a
    # file that is already gone is a document nobody can open and nobody can
    # explain.
    store.delete_document(document_id)
    file_removed = False
    if doc.get("source_uri") and files.available():
        file_removed = await asyncio.to_thread(files.remove, doc["source_uri"])
    log.info("db: deleted document %s (%s)", document_id, doc["filename"])
    return {"document_id": document_id, "deleted": True,
            "filename": doc["filename"], "file_removed": file_removed}


@router.delete("/projects/{project_id}")
async def remove_project(
    project_id: str,
    hard: bool = Query(False, description="Destroy the job and everything "
                                          "under it, including its stored "
                                          "files and its spend history."),
    _key: str = Depends(require_api_key),
):
    """Archive a job, or destroy it and everything under it.

    A hard delete also takes the `run_log` rows -- what each read cost and how
    long it took. That is the only record of money actually spent on this job,
    it cannot be reconstructed, and it is usually wanted long after the drawings
    are not. Archive unless you specifically mean to lose it.
    """
    db = _db()
    found = (db.table("projects").select("id, name")
             .eq("id", project_id).execute())
    if not found.data:
        raise HTTPException(status_code=404, detail="No such project.")
    name = found.data[0]["name"]

    if not hard:
        store.set_project_status(project_id, "archived")
        return {"project_id": project_id, "archived": True, "name": name}

    keys = store.stored_keys(project_id)
    store.delete_project(project_id)
    removed = 0
    if keys and files.available():
        for key in keys:
            if await asyncio.to_thread(files.remove, key):
                removed += 1
    log.info("db: deleted project %s (%s) and %d file(s)",
             project_id, name, removed)
    return {"project_id": project_id, "deleted": True, "name": name,
            "files_removed": removed, "files_found": len(keys)}


# `doors_current` keeps the extracted value and publishes the corrected one
# beside it. The audit should reconcile against what a person has since fixed,
# not against what the extractor first read.
_CORRECTED = {"door_width": "width", "door_height": "height",
              "door_type": "type", "door_material": "material"}


def _stored_rows(db, document_id: str) -> list[DoorRow]:
    """The schedule as it stands now, back in the shape the audit expects."""
    stored = (db.table("doors_current").select("*")
              .eq("document_id", document_id)
              .order("row_index").execute().data)
    rows = []
    for row in stored:
        data = {k: v for k, v in row.items()
                if k in DoorRow.model_fields and v is not None}
        for field, corrected in _CORRECTED.items():
            if row.get(corrected) is not None:
                data[field] = row[corrected]
        rows.append(DoorRow(**data))
    return rows


@router.post("/documents/{document_id}/audit")
async def audit_document(
    document_id: str,
    detect: bool = Query(False, description="Also find doors as shapes with "
                                            "the vision model. This is the "
                                            "only part that costs money."),
    budget_usd: float | None = Query(None, gt=0,
                                     description="Ceiling for this request"),
    _key: str = Depends(require_api_key),
):
    """Locate a stored schedule's doors on the plans, as a separate call.

    Reading the table is steady at about ten seconds whatever the set; locating
    the doors runs from thirty seconds to over two minutes and is what the wait
    is actually made of. Uploading with `plans=false` and calling this after
    puts the schedule on screen as soon as it exists, instead of holding it back
    for the slower half.

    The schedule is not read again. The stored rows are what get located, so
    this cannot disagree with the table already on screen -- and a correction
    made in between is the number it goes looking for.
    """
    db = _db()
    found = store.stored_pdf(document_id)
    if not found:
        raise HTTPException(status_code=404, detail="No such document.")
    key = found.get("source_uri")
    if not key:
        raise HTTPException(
            status_code=409,
            detail="That set's file was not kept, so its plans cannot be read. "
                   "Only sets uploaded through /uploads can be audited later.")
    if not files.available():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="No file store configured.")

    rows = _stored_rows(db, document_id)
    if not rows:
        raise HTTPException(
            status_code=422,
            detail="That document has no doors stored, so there is nothing to "
                   "locate. Read the schedule first.")

    pages = (db.table("schedules").select("page")
             .eq("document_id", document_id).order("page").execute().data)

    path = await asyncio.to_thread(files.fetch, key)
    try:
        result = await in_worker(audit(
            path, rows=rows, detect=detect, budget_usd=budget_usd,
            schedule_page=pages[0]["page"] if pages else None))
    except NotAPdfError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        files.discard(path)

    # The audit is worth returning even when it could not be written down --
    # the caller asked where the doors are, and a storage failure is not an
    # answer to that question.
    saved = store.save_audit(document_id, result)
    return {"document_id": document_id, "doors": len(rows),
            "located_on_plans": len(result.located),
            "not_on_plans": len(result.not_on_plans),
            "sheets_scanned": len(result.floor_plans),
            "duration_ms": result.duration_ms,
            "stored": saved, "warnings": result.warnings,
            "coverage_note": result.coverage_note}


@router.post("/documents/{document_id}/restore")
async def restore_document(document_id: str,
                           _key: str = Depends(require_api_key)):
    """Put an archived drawing set back. Nothing was lost, so nothing re-runs."""
    _db()
    if not store.set_document_status(document_id, "active"):
        raise HTTPException(status_code=404, detail="No such document.")
    return {"document_id": document_id, "archived": False}


@router.post("/projects/{project_id}/restore")
async def restore_project(project_id: str,
                          _key: str = Depends(require_api_key)):
    """Put an archived job back, documents included."""
    _db()
    if not store.set_project_status(project_id, "active"):
        raise HTTPException(status_code=404, detail="No such project.")
    return {"project_id": project_id, "archived": False}


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
