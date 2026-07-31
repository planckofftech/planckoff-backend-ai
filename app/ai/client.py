"""OpenRouter client. One provider only -- the same one the existing app uses."""

from __future__ import annotations

from functools import lru_cache

from openai import AsyncOpenAI

from app.config import get_settings


class AiUnavailableError(RuntimeError):
    """No key configured. The AI tier is skipped, which is not a failure."""


class AiUpstreamError(RuntimeError):
    """OpenRouter refused. Billing and auth failures must surface as themselves,
    never as 'no rows found'."""


@lru_cache
def get_client() -> AsyncOpenAI:
    settings = get_settings()
    if not settings.ai_enabled:
        raise AiUnavailableError("OPENROUTER_API_KEY is not set")
    return AsyncOpenAI(
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        timeout=120.0,
        # 429s and provider 5xx are routine and transient. One retry gave up
        # too early and surfaced a passing hiccup as a hard 502; the SDK backs
        # off between attempts, so a few more cost nothing when calls succeed.
        max_retries=4,
    )
