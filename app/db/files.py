"""Where the drawing sets live.

Cloudflare R2, reached with boto3 -- it speaks the S3 API, so this is the AWS
client pointed somewhere else. Chosen over S3 for one reason that matters here:
S3 bills for every byte read back, and the plan viewer re-renders sheets from
the original PDF every time somebody opens one. R2's egress is free, so looking
at a drawing costs nothing.

Two things follow from the files being here rather than in a request:

  no size limit anywhere      the browser uploads straight to R2 with a signed
                              link, so a 115 MB set never passes through the
                              API. That is what lets this run on a host with a
                              32 MB request cap.

  the file outlives the read  extraction produces rows, but `/preview` needs
                              the PDF again to draw doors on a sheet. A temp
                              file deleted when the upload finished left the
                              viewer with nothing.

Absent settings are not an error, exactly as with the database: uploads through
the API keep working and only the signed-link route is unavailable.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from app.config import get_settings

log = logging.getLogger(__name__)

_client = None


class NoFileStore(RuntimeError):
    """No R2 settings. Callers fall back to accepting the bytes directly."""


def client():
    """The shared S3 client, made once."""
    global _client
    if _client is not None:
        return _client

    settings = get_settings()
    if not settings.files_enabled:
        raise NoFileStore("R2_ENDPOINT / R2_BUCKET / R2_ACCESS_KEY / "
                          "R2_SECRET_KEY are not set")
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise NoFileStore("the boto3 package is not installed") from exc

    _client = boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        aws_access_key_id=settings.r2_access_key,
        aws_secret_access_key=settings.r2_secret_key,
        # R2 has one region and rejects the bucket-location dance S3 does.
        region_name="auto",
        config=Config(signature_version="s3v4",
                      retries={"max_attempts": 3, "mode": "standard"}),
    )
    log.info("r2: connected to %s bucket %s",
             settings.r2_endpoint, settings.r2_bucket)
    return _client


def available() -> bool:
    try:
        client()
    except NoFileStore:
        return False
    return True


def key_for(project_id: str, sha256: str) -> str:
    """Where a set lives, derived from what it is rather than what it is called.

    Named by content hash, so the same drawings re-uploaded under a new name
    land on the same object instead of filling the bucket with copies. Under
    the project, so one job's files can be listed or deleted on their own.
    """
    return f"{project_id}/{sha256}.pdf"


def upload_url(key: str, content_type: str = "application/pdf") -> str:
    """A link the browser may PUT one file to, and nothing else.

    Signed for a single key and a short window, so the caller never holds a
    credential and cannot reach any other object. This is also what removes the
    path-traversal problem: the client never names a path, it uses a permission
    we issued.
    """
    settings = get_settings()
    return client().generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.r2_bucket, "Key": key,
                "ContentType": content_type},
        ExpiresIn=settings.upload_url_ttl,
    )


def read_url(key: str, seconds: int | None = None) -> str:
    """A link to read one object, for a caller that wants the PDF itself."""
    settings = get_settings()
    return client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.r2_bucket, "Key": key},
        ExpiresIn=seconds or settings.upload_url_ttl,
    )


def exists(key: str) -> bool:
    settings = get_settings()
    try:
        client().head_object(Bucket=settings.r2_bucket, Key=key)
    except Exception:  # noqa: BLE001 - a missing object is the common answer
        return False
    return True


def size_of(key: str) -> int:
    settings = get_settings()
    head = client().head_object(Bucket=settings.r2_bucket, Key=key)
    return int(head.get("ContentLength", 0))


def fetch(key: str) -> Path:
    """Bring an object down to a temp file and return its path.

    To disk rather than into memory, for the same reason uploads are spooled:
    PyMuPDF reads pages from a file as it needs them, so a 500 MB set never
    has to be held whole. The caller is responsible for deleting it -- see
    `discard`.
    """
    settings = get_settings()
    handle = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    path = Path(handle.name)
    handle.close()
    client().download_file(settings.r2_bucket, key, str(path))
    log.info("r2: fetched %s (%.1f MB)", key, path.stat().st_size / 1048576)
    return path


def put(key: str, path: Path) -> None:
    """Send a local file up, for callers that received the bytes themselves."""
    settings = get_settings()
    client().upload_file(str(path), settings.r2_bucket, key,
                         ExtraArgs={"ContentType": "application/pdf"})
    log.info("r2: stored %s (%.1f MB)", key, path.stat().st_size / 1048576)


def remove(key: str) -> bool:
    """Delete one stored set. True if it is gone, False if it could not be.

    Safe to call for a document because a key is `{project}/{sha256}.pdf` and
    `documents` is unique on (project_id, sha256): one row, one object. The
    same bytes in another project are a different key and are untouched.

    Storage is the reason this exists at all. Without it a job deleted in the
    morning still costs for its 120 MB every month afterwards, and nothing in
    the product ever mentions it again -- the bucket only grows.
    """
    settings = get_settings()
    try:
        client().delete_object(Bucket=settings.r2_bucket, Key=key)
    except Exception as exc:  # noqa: BLE001 - boto3 raises its own hierarchy
        # Not fatal. The row is going either way; a file left behind is a
        # cleanup job, while refusing the delete leaves a job nobody can remove.
        log.warning("r2: could not delete %s: %s", key, exc)
        return False
    log.info("r2: deleted %s", key)
    return True


def discard(path: Path) -> None:
    """Delete a fetched temp file, tolerating a lock we cannot break."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("r2: could not remove temp file %s: %s", path, exc)
