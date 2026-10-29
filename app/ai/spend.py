"""What one schedule read spent on the model, added up across its calls.

A read makes calls from several places -- the image reader, the header mapper,
once per sheet -- and none of them returned what it cost. The run log recorded
`ai_vision` with no model and $0, so a read that had paid for three sheet
images looked exactly like one that never called the model at all.

Held in a context variable so the callers keep their signatures: `extract`
opens a tally, every call adds to it, and `extract` puts the total on its
result. Outside a read nothing is open and adding is a no-op -- the plan pass
counts its own spend in `ScanCost`.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field

from app.ai.pricing import cost_usd


@dataclass(slots=True)
class Spend:
    models: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usd: float = 0.0

    @property
    def model(self) -> str:
        return ", ".join(self.models)


_OPEN: ContextVar[Spend | None] = ContextVar("ai_spend", default=None)


def start() -> Spend:
    """Open a tally for this read. Calls made from here on are counted on it."""
    tally = Spend()
    _OPEN.set(tally)
    return tally


def add(model: str, usage) -> None:
    """Count one call's tokens against the open tally, priced on its own model."""
    tally = _OPEN.get()
    if tally is None or usage is None:
        return
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    completion = getattr(usage, "completion_tokens", 0) or 0
    if model and model not in tally.models:
        tally.models.append(model)
    tally.prompt_tokens += prompt
    tally.completion_tokens += completion
    tally.usd += cost_usd(model, prompt, completion)
