"""Multi-cluster demo benchmark: round summary and warehouse lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from experiments.multicluster_demo.core import benchmark

BASE = datetime(2026, 8, 1, tzinfo=UTC)


def test_run_round_summarizes_from_stats_and_run(make_cursor, make_connection):
    # Admin cursor returns the tagged-query stats for read_round_stats.
    admin_cursor = make_cursor(
        fetch=[
            (1, 1000, 3000, BASE, BASE, "SUCCESS"),
            (2, 2000, 3000, BASE, BASE, "SUCCESS"),
        ]
    )
    admin_conn = make_connection(admin_cursor)

    def connect():
        return make_connection(make_cursor(fetch=[("ok",)]))

    result = benchmark.run_round(
        connect,
        admin_conn,
        label="multi",
        warehouse="WH",
        database="DB",
        size_query="SELECT 1",
        concurrency=2,
        max_clusters=3,
        query_tag="tag",
    )

    assert result.clusters_used == 2  # distinct cluster numbers 1 and 2
    assert result.query_count == 2  # from the concurrent run
    assert result.failures == 0
    assert result.queue_p50_ms == pytest.approx(1500.0)  # median of [1000, 2000]


def test_managed_warehouse_drops_even_on_error(make_cursor, make_connection):
    cursor = make_cursor()
    admin_conn = make_connection(cursor)

    with pytest.raises(RuntimeError, match="boom"):
        with benchmark.managed_demo_warehouse(admin_conn, name="WH", size="XSMALL"):
            raise RuntimeError("boom")

    assert any("CREATE WAREHOUSE" in s for s in cursor.executed)
    assert any("DROP WAREHOUSE" in s for s in cursor.executed)


def test_run_demo_creates_and_drops_and_returns_two_rounds(make_cursor, make_connection):
    admin_cursor = make_cursor(fetch=[(1, 500, 2000, BASE, BASE, "SUCCESS")])
    admin_conn = make_connection(admin_cursor)

    def connect():
        return make_connection(make_cursor(fetch=[("ok",)]))

    single, multi = benchmark.run_demo(
        connect,
        admin_conn,
        warehouse="WH",
        size="XSMALL",
        concurrency=1,
        max_clusters=3,
        table="DB.SCH.TBL",
        run_token="tok",
    )

    assert single.label == "single cluster" and single.max_clusters == 1
    assert multi.max_clusters == 3
    assert any("CREATE WAREHOUSE" in s for s in admin_cursor.executed)
    assert any("DROP WAREHOUSE" in s for s in admin_cursor.executed)
