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
    # The price-book candidates (AI-Model-Choice-and-Costs, OpenRouter list
    # prices on 30 October 2026).
    "qwen/qwen3.7-flash": (0.03, 0.13),
    "google/gemini-2.5-flash-lite": (0.10, 0.40),
    "bytedance-seed/seed-2.0-mini": (0.10, 0.40),
    "anthropic/claude-haiku-5.5": (0.10, 0.50),
    "openai/gpt-6-luna": (0.10, 0.50),
    "qwen/qwen3.8-flash": (0.15, 0.47),
    "z-ai/glm-5.3-flash": (0.15, 0.50),
    "mistralai/mistral-small-2603": (0.15, 0.60),
    "qwen/qwen3.7-plus": (0.32, 1.28),
    "mistralai/mistral-large-4-0": (0.70, 2.10),
    "google/gemini-3.8-flash": (0.825, 4.125),
    "openai/gpt-6.1-terra": (1.00, 5.00),
    "x-ai/grok-4.7": (2.00, 6.00),
    "qwen/qwen3.8-max-0902": (2.00, 6.00),
    "anthropic/claude-sonnet-5.5": (2.00, 10.00),
    "google/gemini-4-argon": (2.00, 10.00),
    "openai/gpt-6.1-sol": (2.00, 10.00),
    "anthropic/claude-opus-5.5": (4.00, 20.00),
    "anthropic/claude-fable-5.1": (10.00, 50.00),
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
