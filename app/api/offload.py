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
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")


async def in_worker(work: Coroutine[Any, Any, T]) -> T:
    """Await `work` on a worker thread, leaving this event loop free.

    The coroutine is built by the caller and run by `asyncio.run` in the thread,
    which gives it a loop of its own. Safe for the AI tier because its client is
    constructed per call rather than bound to a loop at import.
    """
    return await asyncio.to_thread(asyncio.run, work)
