"""Multi-cluster demo SQL: workload, warehouse DDL, and read-back."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from experiments.multicluster_demo.core import queries

BASE = datetime(2026, 8, 1, tzinfo=UTC)


def test_workload_query_targets_table_and_rejects_unsafe():
    assert "FROM DB.SCH.TBL" in queries.workload_query("DB.SCH.TBL")
    with pytest.raises(ValueError, match="Unsafe table name"):
        queries.workload_query("DB; DROP TABLE X")


def test_database_of():
    assert queries.database_of("DB.SCH.TBL") == "DB"


def test_create_warehouse_sql(make_cursor, make_connection):
    cursor = make_cursor()
    queries.create_warehouse(make_connection(cursor), name="DEMO_WH", size="XSMALL")

    sql = cursor.executed[0]
    assert 'CREATE WAREHOUSE IF NOT EXISTS "DEMO_WH"' in sql
    assert "WAREHOUSE_SIZE = 'XSMALL'" in sql
    assert "MAX_CLUSTER_COUNT = 1" in sql
    assert "MAX_CONCURRENCY_LEVEL = 4" in sql
    assert "STATEMENT_TIMEOUT_IN_SECONDS = 120" in sql
    assert "INITIALLY_SUSPENDED = TRUE" in sql


def test_set_cluster_bounds_sql(make_cursor, make_connection):
    cursor = make_cursor()
    queries.set_cluster_bounds(make_connection(cursor), name="DEMO_WH", min_clusters=1, max_clusters=3)

    assert 'ALTER WAREHOUSE "DEMO_WH" SET MIN_CLUSTER_COUNT = 1 MAX_CLUSTER_COUNT = 3' in cursor.executed[0]


def test_session_setup_quotes_warehouse_and_tag():
    setup = queries.session_setup(warehouse="DEMO_WH", query_tag="run'1")

    assert setup == [
        'USE WAREHOUSE "DEMO_WH"',
        "ALTER SESSION SET QUERY_TAG = 'run''1'",
        "ALTER SESSION SET USE_CACHED_RESULT = FALSE",
    ]


def test_read_round_stats_maps_rows(make_cursor, make_connection):
    cursor = make_cursor(
        fetch=[
            (2, 1500, 3000, BASE, BASE, "SUCCESS"),
            (None, 0, 100, BASE, BASE, "SUCCESS"),
        ]
    )

    stats = queries.read_round_stats(make_connection(cursor), database="DB", warehouse="DEMO_WH", query_tag="tag")

    sql = cursor.executed[0]
    assert '"DB".INFORMATION_SCHEMA.QUERY_HISTORY' in sql
    assert "QUERY_TAG = 'tag'" in sql
    assert stats[0].cluster_number == 2 and stats[0].queued_overload_ms == 1500
    assert stats[1].cluster_number is None
