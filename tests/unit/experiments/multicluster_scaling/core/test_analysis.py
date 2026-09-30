"""Multi-cluster analysis: lifetimes, utilization, and the scaling description."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from experiments.multicluster_scaling.core import analysis
from experiments.multicluster_scaling.core.queries import (
    ClusterEvent,
    ClusterEventKind,
    QueryRecord,
)

BASE = datetime(2026, 8, 1, tzinfo=UTC)


def _at(seconds: int) -> datetime:
    return BASE + timedelta(seconds=seconds)


def _event(warehouse: str, cluster: int, kind: ClusterEventKind, seconds: int) -> ClusterEvent:
    return ClusterEvent(warehouse, cluster, kind, _at(seconds))


def _query(warehouse: str, cluster: int, start_s: int, end_s: int) -> QueryRecord:
    return QueryRecord(warehouse, cluster, _at(start_s), _at(end_s))


def test_pair_cluster_lifetimes_matches_start_to_next_stop():
    events = [
        _event("WH", 2, ClusterEventKind.STARTED, 0),
        _event("WH", 2, ClusterEventKind.STOPPED, 300),
    ]

    (lifetime,) = analysis.pair_cluster_lifetimes(events)

    assert lifetime.cluster_number == 2
    assert lifetime.duration_seconds == 300


def test_pair_cluster_lifetimes_drops_unclosed_intervals():
    events = [
        _event("WH", 2, ClusterEventKind.STARTED, 0),
        _event("WH", 2, ClusterEventKind.STOPPED, 120),
        _event("WH", 3, ClusterEventKind.STARTED, 10),  # never stopped
    ]

    lifetimes = analysis.pair_cluster_lifetimes(events)

    assert [life.cluster_number for life in lifetimes] == [2]


def test_summarize_reports_percentiles_and_none_for_empty():
    distribution = analysis.summarize([10.0, 20.0, 30.0])

    assert distribution is not None
    assert (distribution.count, distribution.minimum, distribution.p50, distribution.maximum) == (
        3,
        10.0,
        20.0,
        30.0,
    )
    assert analysis.summarize([]) is None


def test_cluster_utilization_counts_overlap_once_for_occupancy():
    (lifetime,) = analysis.pair_cluster_lifetimes(
        [
            _event("WH", 2, ClusterEventKind.STARTED, 0),
            _event("WH", 2, ClusterEventKind.STOPPED, 100),
        ]
    )
    queries = [_query("WH", 2, 0, 50), _query("WH", 2, 25, 75)]  # overlap in [25, 50]

    utilization = analysis.cluster_utilization(lifetime, queries)

    assert utilization.query_count == 2
    assert utilization.occupancy == pytest.approx(0.75)  # union [0,75] of 100s
    assert utilization.average_concurrency == pytest.approx(1.0)  # 100 busy-seconds / 100s


def test_describe_reports_peak_and_spinups():
    events = [
        _event("WH", 2, ClusterEventKind.STARTED, 0),
        _event("WH", 2, ClusterEventKind.STOPPED, 120),
        _event("WH", 3, ClusterEventKind.STARTED, 10),
        _event("WH", 3, ClusterEventKind.STOPPED, 90),
    ]

    scaling = analysis.describe_warehouse("WH", analysis.pair_cluster_lifetimes(events), [])

    assert scaling.peak_clusters == 3
    assert scaling.marginal_spinups == 2  # clusters 2 and 3 each ran once


def test_describe_short_idle_clusters():
    events = [
        _event("WH", 2, ClusterEventKind.STARTED, 0),
        _event("WH", 2, ClusterEventKind.STOPPED, 120),
    ]

    scaling = analysis.describe_warehouse("WH", analysis.pair_cluster_lifetimes(events), [_query("WH", 2, 0, 10)])

    assert "idle" in scaling.behavior


def test_describe_long_busy_clusters():
    events = [
        _event("WH", 2, ClusterEventKind.STARTED, 0),
        _event("WH", 2, ClusterEventKind.STOPPED, 1200),
    ]

    scaling = analysis.describe_warehouse("WH", analysis.pair_cluster_lifetimes(events), [_query("WH", 2, 0, 1200)])

    assert "stay busy" in scaling.behavior


def test_describe_warehouse_that_never_scaled():
    events = [
        _event("WH", 1, ClusterEventKind.STARTED, 0),
        _event("WH", 1, ClusterEventKind.STOPPED, 300),
    ]

    scaling = analysis.describe_warehouse("WH", analysis.pair_cluster_lifetimes(events), [])

    assert scaling.peak_clusters == 1
    assert scaling.marginal_spinups == 0
    assert "Never scaled past its base cluster" in scaling.behavior


def test_summarize_scaling_covers_every_warehouse():
    events = [
        _event("WH_A", 2, ClusterEventKind.STARTED, 0),
        _event("WH_A", 2, ClusterEventKind.STOPPED, 120),
        _event("WH_B", 3, ClusterEventKind.STARTED, 0),
        _event("WH_B", 3, ClusterEventKind.STOPPED, 1200),
    ]
    queries = [_query("WH_A", 2, 0, 10), _query("WH_B", 3, 0, 1200)]

    by_name = {s.warehouse: s for s in analysis.summarize_scaling(events, queries)}

    assert set(by_name) == {"WH_A", "WH_B"}
    assert by_name["WH_B"].peak_clusters == 3
