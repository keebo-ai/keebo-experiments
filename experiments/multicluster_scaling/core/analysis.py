"""Describe how a multi-cluster warehouse scaled, from history records.

Pure functions over the records in :mod:`.queries` — no I/O, no ``click`` — so
every calculation here is unit-tested directly. This experiment is descriptive:
it reports *how* Snowflake's multi-cluster scaling behaved (how many clusters
ran, how often extra ones spun up, how long they lived, how busy they were), not
whether you should change anything.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime

from experiments.multicluster_scaling.core.queries import (
    ClusterEvent,
    ClusterEventKind,
    QueryRecord,
)

# Cluster 1 runs whenever the warehouse is up; clusters at or above this are the
# marginal clusters Snowflake adds and removes as load changes.
MARGINAL_CLUSTER_FLOOR = 2

# Thresholds used only to phrase the behavior description in plain language.
SHORT_LIFETIME_SECONDS = 600.0  # 10 minutes
LOW_OCCUPANCY = 0.5  # busy less than half the time it was up


@dataclass(frozen=True)
class ClusterLifetime:
    """One interval during which a single cluster was running."""

    warehouse: str
    cluster_number: int
    started_at: datetime
    stopped_at: datetime

    @property
    def duration_seconds(self) -> float:
        return (self.stopped_at - self.started_at).total_seconds()


def pair_cluster_lifetimes(events: list[ClusterEvent]) -> list[ClusterLifetime]:
    """Pair each cluster start with the next stop for the same cluster.

    Grouped by ``(warehouse, cluster_number)`` and walked in time order: a
    ``STARTED`` opens an interval and the next ``STOPPED`` closes it. A start
    with no matching stop is dropped rather than guessed at, so every returned
    lifetime is a real, closed interval.
    """
    ordered = sorted(events, key=lambda event: (event.warehouse, event.cluster_number, event.at))
    lifetimes: list[ClusterLifetime] = []
    open_start: dict[tuple[str, int], datetime] = {}
    for event in ordered:
        key = (event.warehouse, event.cluster_number)
        if event.kind is ClusterEventKind.STARTED:
            open_start[key] = event.at
        elif key in open_start:  # a STOPPED that closes an open interval
            lifetimes.append(ClusterLifetime(event.warehouse, event.cluster_number, open_start.pop(key), event.at))
    return lifetimes


@dataclass(frozen=True)
class Distribution:
    """A compact summary of a list of numbers."""

    count: int
    minimum: float
    p50: float
    p90: float
    maximum: float
    mean: float


def summarize(values: list[float]) -> Distribution | None:
    """Summarize ``values`` (returns ``None`` for an empty list)."""
    if not values:
        return None
    ordered = sorted(values)
    return Distribution(
        count=len(ordered),
        minimum=ordered[0],
        p50=_percentile(ordered, 0.50),
        p90=_percentile(ordered, 0.90),
        maximum=ordered[-1],
        mean=statistics.fmean(ordered),
    )


def _percentile(ordered: list[float], fraction: float) -> float:
    """Linear-interpolation percentile over an already-sorted list."""
    if len(ordered) == 1:
        return ordered[0]
    rank = fraction * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


@dataclass(frozen=True)
class ClusterUtilization:
    """How busy one cluster was over its lifetime."""

    lifetime: ClusterLifetime
    query_count: int
    # Fraction of the lifetime with at least one query running (0..1).
    occupancy: float
    # Query-seconds over lifetime-seconds; can exceed 1 when queries overlap.
    average_concurrency: float


def cluster_utilization(lifetime: ClusterLifetime, queries: list[QueryRecord]) -> ClusterUtilization:
    """Measure how busy ``lifetime``'s cluster was, from the queries on it."""
    span_seconds = lifetime.duration_seconds
    on_this_cluster = [
        query
        for query in queries
        if query.warehouse == lifetime.warehouse and query.cluster_number == lifetime.cluster_number
    ]
    clipped = [
        (max(query.started_at, lifetime.started_at), min(query.ended_at, lifetime.stopped_at))
        for query in on_this_cluster
    ]
    clipped = [(start, end) for start, end in clipped if end > start]

    busy_seconds = sum((end - start).total_seconds() for start, end in clipped)
    occupied_seconds = _union_seconds(clipped)
    return ClusterUtilization(
        lifetime=lifetime,
        query_count=len(clipped),
        occupancy=(occupied_seconds / span_seconds) if span_seconds > 0 else 0.0,
        average_concurrency=(busy_seconds / span_seconds) if span_seconds > 0 else 0.0,
    )


def _union_seconds(intervals: list[tuple[datetime, datetime]]) -> float:
    """Total length of the union of ``intervals`` (overlaps counted once)."""
    if not intervals:
        return 0.0
    ordered = sorted(intervals)
    total = 0.0
    current_start, current_end = ordered[0]
    for start, end in ordered[1:]:
        if start > current_end:
            total += (current_end - current_start).total_seconds()
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    total += (current_end - current_start).total_seconds()
    return total


@dataclass(frozen=True)
class WarehouseScaling:
    """How one warehouse's multi-cluster scaling behaved over the window."""

    warehouse: str
    peak_clusters: int
    marginal_spinups: int
    lifetime_seconds: Distribution | None
    occupancy: Distribution | None
    behavior: str


def _describe_behavior(lifetime_seconds: Distribution | None, occupancy: Distribution | None) -> str:
    """A neutral, plain-language description of the observed scaling pattern."""
    short_lived = lifetime_seconds is not None and lifetime_seconds.p50 < SHORT_LIFETIME_SECONDS
    mostly_idle = occupancy is not None and occupancy.p50 < LOW_OCCUPANCY
    if short_lived and mostly_idle:
        return (
            "Marginal clusters are short-lived and spend most of their life idle before Snowflake scales them back in."
        )
    if not short_lived and not mostly_idle:
        return "Marginal clusters run for long stretches and stay busy while up."
    if not short_lived:
        return "Marginal clusters run for long stretches but are mostly idle."
    return "Marginal clusters are short-lived but busy for most of the time they run."


def describe_warehouse(
    warehouse: str,
    lifetimes: list[ClusterLifetime],
    queries: list[QueryRecord],
) -> WarehouseScaling:
    """Describe one warehouse from its cluster lifetimes and its queries."""
    peak_clusters = max((life.cluster_number for life in lifetimes), default=1)
    marginal = [life for life in lifetimes if life.cluster_number >= MARGINAL_CLUSTER_FLOOR]
    if not marginal:
        return WarehouseScaling(
            warehouse=warehouse,
            peak_clusters=peak_clusters,
            marginal_spinups=0,
            lifetime_seconds=None,
            occupancy=None,
            behavior="Never scaled past its base cluster in this window.",
        )

    lifetime_seconds = summarize([life.duration_seconds for life in marginal])
    occupancy = summarize([cluster_utilization(life, queries).occupancy for life in marginal])
    return WarehouseScaling(
        warehouse=warehouse,
        peak_clusters=peak_clusters,
        marginal_spinups=len(marginal),
        lifetime_seconds=lifetime_seconds,
        occupancy=occupancy,
        behavior=_describe_behavior(lifetime_seconds, occupancy),
    )


def summarize_scaling(
    events: list[ClusterEvent],
    queries: list[QueryRecord],
    warehouses: tuple[str, ...] | None = None,
) -> list[WarehouseScaling]:
    """Describe every warehouse seen in ``events`` (or only ``warehouses``)."""
    lifetimes = pair_cluster_lifetimes(events)
    names = sorted(warehouses) if warehouses else sorted({life.warehouse for life in lifetimes})
    return [
        describe_warehouse(
            name,
            [life for life in lifetimes if life.warehouse == name],
            [query for query in queries if query.warehouse == name],
        )
        for name in names
    ]
