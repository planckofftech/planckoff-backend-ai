"""Every AI call made to price hardware, as a record kept for the cost report.

The question the report answers is what pricing with AI costs -- per call, per
page sent, per product, per maker, per project and per model -- and how often
each model was right. Tokens alone do not say it: an image costs differently on
each provider, so each call keeps its own tokens and dollars as billed.

Held in a context variable like app/ai/spend.py, so the callers keep their
signatures: a run opens a log, each call adds to it, the run stores it.
Outside an open log a call is not recorded.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class Call:
    step: str                      # identify | A | B
    model: str
    manufacturer: str = ""
    books: list[str] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)
    product_keys: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usd: float = 0.0
    seconds: float = 0.0
    outcome: str = "ok"            # ok | cut_off | error


_OPEN: ContextVar[list[Call] | None] = ContextVar("price_calls", default=None)


def start() -> list[Call]:
    calls: list[Call] = []
    _OPEN.set(calls)
    return calls


def add(call: Call) -> None:
    calls = _OPEN.get()
    if calls is not None:
        calls.append(call)


class Timer:
    def __init__(self) -> None:
        self.started = time.monotonic()

    @property
    def seconds(self) -> float:
        return round(time.monotonic() - self.started, 2)
