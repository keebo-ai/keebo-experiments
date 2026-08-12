"""Drive genuinely concurrent load against a warehouse.

Concurrency here is always **N independent client sessions** hitting the
warehouse at the same instant — never one query fanned out with a generator,
``UNNEST``, or ``GENERATOR(ROWCOUNT => ...)``. Faked concurrency never exercises
admission control, queuing, or cluster scale-out, so it would not show what a
multi-cluster warehouse actually does.

The :class:`threading.Barrier` is what makes the N queries truly overlap: every
worker opens its own connection and runs any per-session ``setup`` first, then
all workers wait on the barrier and are released together. The batch wall-clock
(barrier release to the last query finishing) is measured client-side, so the
headline result never depends on server-side history views.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

# A callable that opens and returns a new, independent connection.
Connect = Callable[[], Any]


@dataclass(frozen=True)
class QueryOutcome:
    """The client-side result of one worker's query (perf_counter seconds)."""

    worker: int
    released_at: float
    finished_at: float
    error: str | None

    @property
    def wall_ms(self) -> float:
        return (self.finished_at - self.released_at) * 1000


@dataclass(frozen=True)
class ConcurrentRun:
    """The result of running ``query`` on ``concurrency`` real sessions."""

    query: str
    concurrency: int
    outcomes: list[QueryOutcome]

    @property
    def failures(self) -> list[QueryOutcome]:
        return [outcome for outcome in self.outcomes if outcome.error is not None]

    @property
    def wall_clock_s(self) -> float:
        """Seconds from the barrier release to the last query finishing."""
        if not self.outcomes:
            return 0.0
        return max(o.finished_at for o in self.outcomes) - min(o.released_at for o in self.outcomes)


def run_concurrent(
    connect: Connect,
    query: str,
    concurrency: int,
    *,
    setup: Sequence[str] = (),
) -> ConcurrentRun:
    """Run ``query`` on ``concurrency`` independent sessions, released together.

    Each worker opens its own connection via ``connect()``, runs the ``setup``
    statements (e.g. ``USE WAREHOUSE``, ``ALTER SESSION SET QUERY_TAG``), waits
    on the barrier, then executes the exact ``query`` — unchanged, one real
    statement per real session.
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")

    barrier = threading.Barrier(concurrency)

    def worker(index: int) -> QueryOutcome:
        conn = connect()
        try:
            cursor = conn.cursor()
            for statement in setup:
                cursor.execute(statement)
            # All sessions are open and set up; fire the queries together.
            barrier.wait()
            released_at = time.perf_counter()
            error: str | None = None
            try:
                cursor.execute(query)
            except Exception as exc:  # noqa: BLE001 - recorded per worker, not swallowed
                error = f"{type(exc).__name__}: {exc}"
            return QueryOutcome(
                worker=index,
                released_at=released_at,
                finished_at=time.perf_counter(),
                error=error,
            )
        finally:
            conn.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        outcomes = [f.result() for f in [pool.submit(worker, i) for i in range(concurrency)]]

    outcomes.sort(key=lambda outcome: outcome.worker)
    return ConcurrentRun(query=query, concurrency=concurrency, outcomes=outcomes)
