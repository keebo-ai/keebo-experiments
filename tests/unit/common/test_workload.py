"""The concurrency driver must drive N real sessions — not fake concurrency."""

from __future__ import annotations

import threading

import pytest

from common.workload import run_concurrent

QUERY = "SELECT SYSTEM$WAIT(1)"


def test_opens_one_real_session_per_worker_and_runs_query_verbatim(make_cursor, make_connection):
    cursors = []

    def connect():
        cursor = make_cursor(fetch=[("ok",)])
        cursors.append(cursor)
        return make_connection(cursor)

    run = run_concurrent(connect, QUERY, concurrency=5, setup=['USE WAREHOUSE "WH"'])

    # Five workers means five distinct sessions, each running setup then the
    # exact query — never one statement faking concurrency.
    assert len(cursors) == 5
    assert len(run.outcomes) == 5
    for cursor in cursors:
        assert cursor.executed == ['USE WAREHOUSE "WH"', QUERY]


def test_reports_no_failures_when_queries_succeed(make_cursor, make_connection):
    def connect():
        return make_connection(make_cursor(fetch=[("ok",)]))

    run = run_concurrent(connect, QUERY, concurrency=4)

    assert run.failures == []
    assert run.wall_clock_s >= 0


def test_rejects_non_positive_concurrency(make_cursor, make_connection):
    def connect():
        return make_connection(make_cursor())

    with pytest.raises(ValueError, match="concurrency must be >= 1"):
        run_concurrent(connect, QUERY, concurrency=0)


def test_one_failed_connect_does_not_deadlock_the_barrier(make_cursor, make_connection):
    # One worker fails to connect before reaching the barrier. Its peers must
    # not block on the barrier forever — the run raises promptly instead.
    lock = threading.Lock()
    calls = {"n": 0}

    def connect():
        with lock:
            calls["n"] += 1
            first = calls["n"] == 1
        if first:
            raise RuntimeError("connect boom")
        return make_connection(make_cursor(fetch=[("ok",)]))

    with pytest.raises((RuntimeError, threading.BrokenBarrierError)):
        run_concurrent(connect, QUERY, concurrency=3)
