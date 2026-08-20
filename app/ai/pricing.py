"""What a model actually costs, and a ceiling that stops before it is exceeded.

This file exists because of a real incident. The cost reported to the user was
computed with Gemini *Flash* prices while the detector was running Gemini
*Pro* -- about four times cheaper on paper than in life. Every estimate quoted
was wrong by that factor, the per-request cap was per *sheet* rather than per
request so a five-sheet project could quietly spend five times the ceiling, and
two projects cost roughly $6 against a stated $0.70.

The lesson is not "pick better numbers". It is that a spend estimate must be
tied to the model actually being called, and that an estimate is not a control.
So there are two things here: prices that are looked up per model, and a hard
budget that aborts.

Prices are USD per million tokens and will drift. When they do, the reported
figure drifts with them -- which is why the response also carries the token
counts, so a real invoice can always be reconciled against what was sent.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    """The run stopped because it reached its spending ceiling.

    Raised rather than returned: a partial scan that silently cost the maximum
    is exactly the outcome this is here to prevent.
    """


# USD per million tokens, (input, output). Approximate and provider-dependent.
_PRICES: dict[str, tuple[float, float]] = {
    "google/gemini-2.5-pro": (1.25, 10.00),
    "google/gemini-2.5-flash": (0.30, 2.50),
    "google/gemini-2.0-flash": (0.10, 0.40),
    "qwen/qwen2.5-vl-72b-instruct": (0.25, 0.75),
    "openai/gpt-4o": (2.50, 10.00),
    "anthropic/claude-sonnet-4.5": (3.00, 15.00),
}
# An unknown model is priced at the dearest rate on the list, never the
# cheapest. Guessing low is how a bill becomes a surprise.
_UNKNOWN = max(_PRICES.values(), key=lambda p: p[1])


def price_of(model: str) -> tuple[float, float]:
    """(input, output) USD per million tokens for this model."""
    known = _PRICES.get((model or "").strip().lower())
    if known is None:
        log.warning("pricing: no rate for %r; assuming the dearest known rate "
                    "%s so the estimate cannot flatter the bill", model, _UNKNOWN)
        return _UNKNOWN
    return known


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """What those tokens cost on that model."""
    rate_in, rate_out = price_of(model)
    return prompt_tokens / 1e6 * rate_in + completion_tokens / 1e6 * rate_out


# Measured, not guessed: one 12-tile scan of a real sheet on gemini-2.5-pro
# billed about 0.039 USD per tile. Used only to predict a run before it starts,
# and to warn when a run is about to be expensive.
USD_PER_TILE = {
    "google/gemini-2.5-pro": 0.039,
    "google/gemini-2.5-flash": 0.010,
}
_DEFAULT_PER_TILE = 0.05


def estimate_usd(model: str, tiles: int) -> float:
    """Roughly what a scan of this many tiles will cost, before running it."""
    return tiles * USD_PER_TILE.get((model or "").strip().lower(), _DEFAULT_PER_TILE)
