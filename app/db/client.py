"""The Supabase connection, made once.

Holds the *service* key, so everything through here bypasses row-level security.
That is correct for this process and wrong for any other: the backend is the
only writer, and it is trusted to decide which organisation a row belongs to.
The anon key -- the one RLS actually applies to -- belongs in a browser.

Absent settings are not an error. The service ran for months with no database
and must keep doing so: a takeoff is still produced and returned, it is simply
not remembered. That way a Supabase outage degrades the product instead of
stopping it.
"""

from __future__ import annotations

import logging

from app.config import get_settings

log = logging.getLogger(__name__)

_client = None


class NoDatabase(RuntimeError):
    """No Supabase settings. Callers skip storing rather than failing."""


def client():
    """The shared client, or NoDatabase if the service is running without one."""
    global _client
    if _client is not None:
        return _client

    settings = get_settings()
    if not settings.db_enabled:
        raise NoDatabase("SUPABASE_URL / SUPABASE_SERVICE_KEY are not set")

    try:
        from supabase import create_client
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise NoDatabase("the supabase package is not installed") from exc

    _client = create_client(settings.supabase_url, settings.supabase_service_key)
    log.info("supabase: connected to %s", settings.supabase_url)
    return _client


def available() -> bool:
    try:
        client()
    except NoDatabase:
        return False
    return True
