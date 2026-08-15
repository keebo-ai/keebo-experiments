"""The shared fakes have enough behaviour to be worth testing directly."""

from __future__ import annotations


def test_cursor_returns_queued_responses_then_falls_back(make_cursor):
    cursor = make_cursor(
        responses=[
            ([("STARTED", 1, 0)], [("state",), ("started_clusters",), ("queued",)]),
            ([("STARTED", 2, 0)], [("state",), ("started_clusters",), ("queued",)]),
        ],
        fetch=[("SUSPENDED", 0, 0)],
        description=[("state",), ("started_clusters",), ("queued",)],
    )

    cursor.execute("SHOW WAREHOUSES LIKE 'W'")
    assert cursor.fetchall() == [("STARTED", 1, 0)]
    cursor.execute("SHOW WAREHOUSES LIKE 'W'")
    assert cursor.fetchall() == [("STARTED", 2, 0)]
    cursor.execute("SHOW WAREHOUSES LIKE 'W'")
    assert cursor.fetchall() == [("SUSPENDED", 0, 0)]
    assert [c[0] for c in cursor.description] == ["state", "started_clusters", "queued"]


def test_execute_async_assigns_distinct_query_ids(make_cursor, make_connection):
    cursor = make_cursor()
    conn = make_connection(cursor)

    first = conn.cursor().execute_async("SELECT 1").sfqid
    second = conn.cursor().execute_async("SELECT 2").sfqid

    assert first != second
    assert cursor.executed == ["SELECT 1", "SELECT 2"]


def test_query_status_drains_then_reports_finished(make_cursor, make_connection):
    conn = make_connection(make_cursor())
    qid = conn.cursor().execute_async("SELECT 1").sfqid
    conn.query_states[qid] = [True, False]

    assert conn.is_still_running(conn.get_query_status(qid)) is True
    assert conn.is_still_running(conn.get_query_status(qid)) is False
    assert conn.is_still_running(conn.get_query_status(qid)) is False
