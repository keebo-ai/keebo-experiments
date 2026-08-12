"""Unit tests for the ACCOUNT_USAGE reporting layer."""

from __future__ import annotations

from experiments.warehouse_sizing_benchmark.core import report


def test_read_report_returns_every_step(make_cursor, make_connection):
    cursor = make_cursor(description=[("executions",), ("distinct_texts",)], fetch=[(18, 1)])
    conn = make_connection(cursor)

    tables = report.read_report(conn, warehouse_name="MY_WH", hours=12)

    assert [t.step for t in tables] == [10, 11, 12, 13, 14, 15, 16]
    assert tables[0].columns == ["executions", "distinct_texts"]
    assert tables[0].rows == [(18, 1)]
    assert any("-12," in s for s in cursor.executed)  # hours interpolated
    assert any("MY_WH" in s for s in cursor.executed)  # warehouse in metering query
    assert cursor.closed
