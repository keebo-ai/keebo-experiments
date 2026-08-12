"""Multi-cluster ACCOUNT_USAGE reads: SQL shape, mapping, and guards."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from experiments.multicluster_scaling.core import queries
from experiments.multicluster_scaling.core.queries import ClusterEventKind

BASE = datetime(2026, 8, 1, tzinfo=UTC)


def test_cluster_events_builds_sql_and_maps_rows(make_cursor, make_connection):
    cursor = make_cursor(
        fetch=[
            (BASE, "WH", 2, "SPINUP_CLUSTER"),
            (BASE, "WH", 2, "SPINDOWN_CLUSTER"),
        ]
    )

    events = queries.cluster_events(make_connection(cursor), days=14)

    sql = cursor.executed[0]
    assert "SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_EVENTS_HISTORY" in sql
    assert "DATEADD('day', -14, CURRENT_TIMESTAMP())" in sql
    assert "WAREHOUSE_NAME IN" not in sql  # no filter when none requested
    assert [event.kind for event in events] == [ClusterEventKind.STARTED, ClusterEventKind.STOPPED]
    assert events[0].warehouse == "WH" and events[0].cluster_number == 2


def test_cluster_events_filters_by_warehouse(make_cursor, make_connection):
    cursor = make_cursor(fetch=[])

    queries.cluster_events(make_connection(cursor), days=7, warehouse_names=("WH_A", "WH_B"))

    assert "WAREHOUSE_NAME IN ('WH_A', 'WH_B')" in cursor.executed[0]


def test_query_history_maps_rows(make_cursor, make_connection):
    cursor = make_cursor(fetch=[("WH", 3, BASE, BASE)])

    records = queries.query_history(make_connection(cursor), days=1)

    assert "SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY" in cursor.executed[0]
    assert records[0].warehouse == "WH" and records[0].cluster_number == 3


def test_use_warehouse_issues_use_statement(make_cursor, make_connection):
    cursor = make_cursor()

    queries.use_warehouse(make_connection(cursor), "MY_WH")

    assert "USE WAREHOUSE MY_WH" in cursor.executed


def test_unsafe_warehouse_name_is_rejected(make_cursor, make_connection):
    conn = make_connection(make_cursor())

    with pytest.raises(ValueError, match="Unsafe warehouse name"):
        queries.cluster_events(conn, days=7, warehouse_names=("bad-name",))

    with pytest.raises(ValueError, match="Unsafe warehouse name"):
        queries.use_warehouse(conn, "bad-name")
