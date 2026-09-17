"""Multi-cluster ACCOUNT_USAGE reads: SQL shape, mapping, and guards."""

from __future__ import annotations

from datetime import UTC, datetime

from experiments.multicluster_scaling.core import queries
from experiments.multicluster_scaling.core.queries import ClusterEventKind

BASE = datetime(2026, 8, 1, tzinfo=UTC)


def test_cluster_events_builds_sql_and_maps_rows(make_cursor, make_connection):
    cursor = make_cursor(
        fetch=[
            (BASE, "WH", 2, "RESUME_CLUSTER"),
            (BASE, "WH", 2, "SUSPEND_CLUSTER"),
        ]
    )

    events = queries.cluster_events(make_connection(cursor), days=14)

    sql = cursor.executed[0]
    assert "SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_EVENTS_HISTORY" in sql
    assert "EVENT_STATE = 'STARTED'" in sql
    assert "CLUSTER_NUMBER IS NOT NULL" in sql
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


def test_use_warehouse_double_quotes_the_identifier(make_cursor, make_connection):
    cursor = make_cursor()

    # Warehouse names may be lower-case or hyphenated (quoted identifiers).
    queries.use_warehouse(make_connection(cursor), "wh-with-dash")

    assert 'USE WAREHOUSE "wh-with-dash"' in cursor.executed


def test_warehouse_filter_quotes_names_as_literals(make_cursor, make_connection):
    cursor = make_cursor(fetch=[])

    # Hyphens are valid names; single quotes are escaped — neither is rejected.
    queries.cluster_events(make_connection(cursor), days=7, warehouse_names=("wh-with-dash", "o'brien"))

    assert "WAREHOUSE_NAME IN ('wh-with-dash', 'o''brien')" in cursor.executed[0]
