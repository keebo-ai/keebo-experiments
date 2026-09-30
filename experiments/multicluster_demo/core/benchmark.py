"""Run the single-cluster vs multi-cluster rounds and summarize each.

Domain layer: no ``click``. The concurrent load comes from the shared
``common.workload`` driver (N real sessions); everything here is orchestration
and arithmetic over the read-back stats.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from common.workload import ConcurrentRun, run_concurrent
from experiments.multicluster_demo.core import queries
from experiments.multicluster_demo.core.queries import QueryStat

# INFORMATION_SCHEMA.QUERY_HISTORY can trail a query finishing by a second or
# two, so retry the read-back until every query shows up (or we give up).
_STATS_READ_ATTEMPTS = 6
_STATS_READ_WAIT_SECONDS = 2.0


@dataclass(frozen=True)
class RoundResult:
    """The outcome of one round (single- or multi-cluster)."""

    label: str
    concurrency: int
    max_clusters: int
    clusters_used: int
    queue_p50_ms: float
    queue_p95_ms: float
    wall_clock_s: float
    query_count: int
    failures: int


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = fraction * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _summarize(
    label: str, *, concurrency: int, max_clusters: int, run: ConcurrentRun, stats: list[QueryStat]
) -> RoundResult:
    queue_times = [float(stat.queued_overload_ms) for stat in stats]
    clusters = {stat.cluster_number for stat in stats if stat.cluster_number is not None}
    return RoundResult(
        label=label,
        concurrency=concurrency,
        max_clusters=max_clusters,
        clusters_used=len(clusters),
        queue_p50_ms=_percentile(queue_times, 0.50),
        queue_p95_ms=_percentile(queue_times, 0.95),
        wall_clock_s=run.wall_clock_s,  # client-side: barrier release -> last finish
        query_count=len(run.outcomes),
        failures=len(run.failures),
    )


def _read_stats(admin_conn: Any, *, database: str, warehouse: str, query_tag: str, expected: int) -> list[QueryStat]:
    """Read the tagged stats, retrying until all queries appear (history lag)."""
    stats = queries.read_round_stats(admin_conn, database=database, warehouse=warehouse, query_tag=query_tag)
    attempts = 1
    while len(stats) < expected and attempts < _STATS_READ_ATTEMPTS:
        time.sleep(_STATS_READ_WAIT_SECONDS)
        stats = queries.read_round_stats(admin_conn, database=database, warehouse=warehouse, query_tag=query_tag)
        attempts += 1
    return stats


def run_round(
    connect: Callable[[], Any],
    admin_conn: Any,
    *,
    label: str,
    warehouse: str,
    database: str,
    size_query: str,
    concurrency: int,
    max_clusters: int,
    query_tag: str,
) -> RoundResult:
    """Set the cluster ceiling, drive N concurrent queries, read the stats back."""
    queries.set_cluster_bounds(admin_conn, name=warehouse, min_clusters=1, max_clusters=max_clusters)
    setup = queries.session_setup(warehouse=warehouse, query_tag=query_tag)
    run = run_concurrent(connect, size_query, concurrency, setup=setup)
    stats = _read_stats(admin_conn, database=database, warehouse=warehouse, query_tag=query_tag, expected=concurrency)
    return _summarize(label, concurrency=concurrency, max_clusters=max_clusters, run=run, stats=stats)


@contextlib.contextmanager
def managed_demo_warehouse(admin_conn: Any, *, name: str, size: str) -> Iterator[None]:
    """Create the demo warehouse, yield, and always drop it on the way out."""
    queries.create_warehouse(admin_conn, name=name, size=size)
    try:
        yield
    finally:
        queries.drop_warehouse(admin_conn, name)


def run_demo(
    connect: Callable[[], Any],
    admin_conn: Any,
    *,
    warehouse: str,
    size: str,
    concurrency: int,
    max_clusters: int,
    table: str,
    run_token: str,
) -> tuple[RoundResult, RoundResult]:
    """Run the single-cluster round then the multi-cluster round on one warehouse."""
    query = queries.workload_query(table)
    database = queries.database_of(table)
    with managed_demo_warehouse(admin_conn, name=warehouse, size=size):
        # Prime the warehouse: resume it and warm the local cache with one query,
        # so both rounds start warm and the comparison reflects clustering rather
        # than cold-start effects.
        run_concurrent(
            connect,
            query,
            1,
            setup=queries.session_setup(warehouse=warehouse, query_tag=f"{run_token}_prime"),
        )
        single = run_round(
            connect,
            admin_conn,
            label="single cluster",
            warehouse=warehouse,
            database=database,
            size_query=query,
            concurrency=concurrency,
            max_clusters=1,
            query_tag=f"{run_token}_single",
        )
        multi = run_round(
            connect,
            admin_conn,
            label=f"multi-cluster (max {max_clusters})",
            warehouse=warehouse,
            database=database,
            size_query=query,
            concurrency=concurrency,
            max_clusters=max_clusters,
            query_tag=f"{run_token}_multi",
        )
    return single, multi
