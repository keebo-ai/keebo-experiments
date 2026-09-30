"""Multi-cluster demo report: comparison table and takeaways."""

from __future__ import annotations

from experiments.multicluster_demo.core.benchmark import RoundResult
from experiments.multicluster_demo.core.report import comparison_table, takeaways

SINGLE = RoundResult(
    label="single cluster",
    concurrency=16,
    max_clusters=1,
    clusters_used=1,
    queue_p50_ms=4000,
    queue_p95_ms=9000,
    wall_clock_s=38.5,
    query_count=16,
    failures=0,
)
MULTI = RoundResult(
    label="multi-cluster (max 3)",
    concurrency=16,
    max_clusters=3,
    clusters_used=3,
    queue_p50_ms=0,
    queue_p95_ms=120,
    wall_clock_s=13.2,
    query_count=16,
    failures=0,
)


def test_comparison_table_has_both_columns_and_key_rows():
    table = comparison_table(SINGLE, MULTI)

    assert table.columns == ["metric", "single cluster", "multi-cluster (max 3)"]
    metrics = [row[0] for row in table.rows]
    assert "clusters used" in metrics
    assert "total wall-clock (s)" in metrics


def test_takeaways_describe_the_benefit():
    lines = "\n".join(takeaways(SINGLE, MULTI))

    assert "3 cluster" in lines
    assert "lower" in lines  # queue-time reduction
    assert "faster" in lines  # wall-clock reduction


def test_takeaways_flag_when_no_extra_cluster():
    no_scale = RoundResult("multi", 16, 3, 1, 4000, 9000, 38.0, 16, 0)
    lines = "\n".join(takeaways(SINGLE, no_scale))

    assert "did not add a cluster" in lines
