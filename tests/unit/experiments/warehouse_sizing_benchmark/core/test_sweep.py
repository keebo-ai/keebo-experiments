"""Unit tests for the sweep / teardown domain layer."""

from __future__ import annotations

import pytest

from experiments.warehouse_sizing_benchmark.core import queries, sweep


def test_sweep_sizes_issues_expected_sql(make_cursor, make_connection):
    cursor = make_cursor(fetch=[("LINEITEM",)])  # sample-data check passes
    conn = make_connection(cursor)

    sweep.sweep_sizes(conn, sizes=[("XSMALL", "X-Small", 1)], runs=2)

    sql = cursor.executed
    assert any("CREATE WAREHOUSE IF NOT EXISTS SIZING_BENCHMARK_WH" in s for s in sql)
    assert "USE WAREHOUSE SIZING_BENCHMARK_WH" in sql
    assert "ALTER SESSION SET USE_CACHED_RESULT = FALSE" in sql
    assert "ALTER WAREHOUSE SIZING_BENCHMARK_WH SET WAREHOUSE_SIZE = XSMALL" in sql
    assert "ALTER SESSION SET QUERY_TAG = 'wsbench:XSMALL:1'" in sql
    assert "ALTER SESSION SET QUERY_TAG = 'wsbench:XSMALL:2'" in sql
    assert sql.count(queries.BENCHMARK_QUERY) == 2  # once per run
    assert "ALTER WAREHOUSE SIZING_BENCHMARK_WH SUSPEND" in sql
    assert cursor.closed


def test_sweep_sizes_reports_progress(make_cursor, make_connection):
    conn = make_connection(make_cursor(fetch=[("LINEITEM",)]))
    messages: list[str] = []

    sweep.sweep_sizes(conn, sizes=[("XSMALL", "X-Small", 1)], runs=1, echo=messages.append)

    joined = "\n".join(messages)
    assert "X-Small" in joined
    assert "run 1 (cold)" in joined


def test_sweep_sizes_missing_sample_data_raises(make_cursor, make_connection):
    conn = make_connection(make_cursor(fetch=[]))  # SHOW returns nothing

    with pytest.raises(ValueError, match="not found"):
        sweep.sweep_sizes(conn, sizes=[("XSMALL", "X-Small", 1)], runs=1)


def test_sweep_sizes_rejects_bad_identifier(make_cursor, make_connection):
    conn = make_connection(make_cursor())

    with pytest.raises(ValueError, match="table"):
        sweep.sweep_sizes(conn, table="bad; DROP TABLE x", runs=1)


def test_drop_warehouse(make_cursor, make_connection):
    cursor = make_cursor()
    conn = make_connection(cursor)
    messages: list[str] = []

    sweep.drop_warehouse(conn, warehouse_name="MY_WH", echo=messages.append)

    assert "DROP WAREHOUSE IF EXISTS MY_WH" in cursor.executed
    assert messages == ["Dropped MY_WH."]
