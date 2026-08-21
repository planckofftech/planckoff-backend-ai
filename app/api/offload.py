"""Run a long takeoff without freezing the server.

`extract` and `audit` are `async def`, but almost everything they do is
synchronous CPU work: PyMuPDF parsing pages, fitting circles to arcs, walking
grids. A coroutine that does not await holds the event loop, and the loop is the
only thread serving requests -- so one upload stops everything.

Measured on one 115 MB set, calling `extract` directly from a route:

    extract           22.2 s
    heartbeat ticks   0 of an expected 1108

Nothing else was served for twenty-two seconds. Not another upload, not a read,
not the health check -- and a load balancer that gets no answer to a health
check will kill the process in the middle of the run.

So the work goes to a worker thread with an event loop of its own. The request
still takes twenty-two seconds; the difference is that everyone else's does not.

This is not a job queue and does not pretend to be. A deploy or a dropped
connection still loses the run, and concurrency is bounded by the default thread
pool. It is the smallest change that makes the server usable by more than one
person at a time, and the queue can replace it without touching the routes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine, TypeVar

from app.config import get_settings

log = logging.getLogger(__name__)

T = TypeVar("T")

_limit: asyncio.Semaphore | None = None


def _gate() -> asyncio.Semaphore:
    """How many sets may be read at once.

    Moving the work off the loop solved one problem and created another: two
    uploads now extract at the same time, and one set peaked at 434 MB. Two of
    those on a 1 GB host is an out-of-memory kill -- and the kernel does not
    stop the offending job, it takes the whole process, losing both runs and
    every request in flight.

    So the second upload waits here instead of competing. It costs that caller
    the length of the first extraction; it saves everyone the process dying.
    Built lazily because a Semaphore binds to the loop that first awaits it.
    """
    global _limit
    if _limit is None:
        _limit = asyncio.Semaphore(get_settings().max_concurrent_extractions)
    return _limit


async def in_worker(work: Coroutine[Any, Any, T]) -> T:
    """Await `work` on a worker thread, leaving this event loop free.

    The coroutine is built by the caller and run by `asyncio.run` in the thread,
    which gives it a loop of its own. Safe for the AI tier because its client is
    constructed per call rather than bound to a loop at import.
    """
    gate = _gate()
    if gate.locked():
        log.info("extraction queued: %d already running",
                 get_settings().max_concurrent_extractions)
    async with gate:
        return await asyncio.to_thread(asyncio.run, work)
