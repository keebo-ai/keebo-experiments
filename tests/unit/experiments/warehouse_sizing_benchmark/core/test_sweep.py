"""Unit tests for the sweep."""

from __future__ import annotations

import itertools

import pytest

from experiments.warehouse_sizing_benchmark.core import infra, queries, sweep

GB = 1024**3
OBJECTS = infra.BenchmarkObjects.named()
XSMALL = ("XSMALL", "X-Small", 1)
MEDIUM = ("MEDIUM", "Medium", 4)


def _clock(step: float = 10.0):
    ticks = itertools.count(0, step)
    return lambda: next(ticks)


def _sweep(conn, **kwargs):
    kwargs.setdefault("sizes", [XSMALL])
    kwargs.setdefault("runs", 2)
    kwargs.setdefault("clock", _clock())
    kwargs.setdefault("sleep", lambda _s: None)
    return sweep.sweep_sizes(conn, objects=OBJECTS, run_id="R1", **kwargs)


def test_sweep_issues_the_articles_sql(account):
    cursor, conn = account()

    _sweep(conn)

    sql = cursor.executed
    assert "SET lineitem_table = 'SNOWFLAKE_SAMPLE_DATA.TPCH_SF100.LINEITEM'" in sql
    assert "USE WAREHOUSE SIZING_BENCHMARK_WH" in sql
    assert "ALTER SESSION SET USE_CACHED_RESULT = FALSE" in sql
    # 3 credits minus the X-Small's 60s minimum, over 2 queries: 5370s each on a Gen1 X-Small.
    assert "ALTER WAREHOUSE SIZING_BENCHMARK_WH SET WAREHOUSE_SIZE = XSMALL STATEMENT_TIMEOUT_IN_SECONDS = 5370" in sql
    assert "ALTER SESSION SET QUERY_TAG = 'wsbench:R1:XSMALL:1'" in sql
    assert "ALTER SESSION SET QUERY_TAG = 'wsbench:R1:XSMALL:2'" in sql
    assert sql.count(queries.BENCHMARK_QUERY) == 2
    assert "ALTER WAREHOUSE SIZING_BENCHMARK_WH SUSPEND" in sql
    assert (
        sql[-1] == "ALTER WAREHOUSE SIZING_BENCHMARK_WH SET WAREHOUSE_SIZE = XSMALL STATEMENT_TIMEOUT_IN_SECONDS = 1800"
    )
    assert cursor.closed


def test_the_tag_is_cleared_before_the_stats_lookups(account):
    cursor, conn = account()
    _sweep(conn, runs=1)
    sql = cursor.executed
    query_at = sql.index(queries.BENCHMARK_QUERY)
    stats_at = next(i for i, s in enumerate(sql) if "QUERY_HISTORY_BY_SESSION" in s)
    assert "ALTER SESSION UNSET QUERY_TAG" in sql[query_at:stats_at]


def test_results_use_snowflake_time_spill_and_the_generation_rate(account):
    _cursor, conn = account(generation="2", spill=(5, 3 * GB, GB // 2), elapsed_ms=42_000)

    [result] = _sweep(conn)

    assert (result.label, result.runtimes_s) == ("X-Small", (42.0, 42.0))
    assert (result.gb_spill_local, result.gb_spill_remote) == (3.0, 0.5)
    assert result.credits_per_hour == pytest.approx(1.35)
    assert result.query_credits == round(42.0 * 1.35 / 3600, 5)
    assert result.billed_credits == round(84.0 * 1.35 / 3600, 5)  # both runs, back to back


def test_short_runs_bill_the_60_second_minimum(account):
    _cursor, conn = account(elapsed_ms=9_000)
    [result] = _sweep(conn, sizes=[MEDIUM], runs=1)
    assert result.query_credits == round(9 * 4 / 3600, 5)
    assert result.billed_credits == round(60 * 4 / 3600, 5)


def test_falls_back_to_client_time_when_history_is_unavailable(account):
    _cursor, conn = account(elapsed_ms=None)
    [result] = _sweep(conn, runs=1, clock=_clock(7.0))
    assert result.runtimes_s == (7.0,)


def test_progress_keeps_the_articles_run_lines(account):
    _cursor, conn = account()
    messages: list[str] = []
    _sweep(conn, echo=messages.append)
    joined = "\n".join(messages)
    assert "=== X-Small (XSMALL) ===" in joined
    assert "run 1 (cold):" in joined and "run 2 (warm):" in joined
    assert "spill: 3.0 GB local, 0.0 GB remote" in joined
    assert "Run id: R1" in joined


class _SnowflakeTimeout(Exception):
    errno = 630


def test_a_capped_query_is_reported_and_skips_its_warm_runs(account):
    cursor, conn = account()
    real_execute = cursor.execute
    state = {"raised": False}

    def execute(sql, *args):
        real_execute(sql, *args)
        if sql == queries.BENCHMARK_QUERY and not state["raised"]:
            state["raised"] = True
            raise _SnowflakeTimeout("Statement reached its statement or warehouse timeout")
        return cursor

    cursor.execute = execute
    messages: list[str] = []

    [result] = _sweep(conn, runs=3, echo=messages.append)

    assert result.timed_out and len(result.runtimes_s) == 1
    assert cursor.executed.count(queries.BENCHMARK_QUERY) == 1
    assert any("stopped by the cost cap" in m for m in messages)


def test_other_errors_propagate_after_suspending_and_resetting(account):
    cursor, conn = account()
    real_execute = cursor.execute

    def execute(sql, *args):
        real_execute(sql, *args)
        if sql == queries.BENCHMARK_QUERY:
            raise RuntimeError("boom")
        if sql.endswith(" SUSPEND"):
            raise RuntimeError("Invalid state. Warehouse cannot be suspended.")
        return cursor

    cursor.execute = execute

    with pytest.raises(RuntimeError, match="boom"):
        _sweep(conn)
    assert cursor.executed[-1].endswith("SET WAREHOUSE_SIZE = XSMALL STATEMENT_TIMEOUT_IN_SECONDS = 1800")


def test_sweep_needs_setup(account):
    cursor, conn = account(warehouse_comment=None)
    with pytest.raises(ValueError, match="warehouse-sizing setup"):
        _sweep(conn)
    assert not any(s.startswith("ALTER WAREHOUSE") for s in cursor.executed)


def test_sweep_refuses_someone_elses_warehouse(account):
    cursor, conn = account(warehouse_comment="production")
    with pytest.raises(ValueError, match="wasn't created by this experiment"):
        _sweep(conn)
    assert not any(s.startswith("ALTER WAREHOUSE") for s in cursor.executed)


def test_the_cap_must_cover_the_minimums(account):
    _cursor, conn = account()
    with pytest.raises(ValueError, match="60-second minimum"):
        _sweep(conn, sizes=list(queries.SIZES), runs=3, max_credits=0.5)


@pytest.mark.parametrize("bad", ["bad'id", "x y"])
def test_run_id_must_be_tag_safe(account, bad):
    _cursor, conn = account()
    with pytest.raises(ValueError, match="run id"):
        sweep.sweep_sizes(conn, objects=OBJECTS, run_id=bad)
