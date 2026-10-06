"""Unit tests for the sweep."""

from __future__ import annotations

import itertools

import pytest

from common import warehouses
from experiments.warehouse_sizing_benchmark.core import infra, queries, sweep

GB = 1024**3
OBJECTS = infra.BenchmarkObjects.named()
IDLE = (
    "ALTER WAREHOUSE SIZING_BENCHMARK_WH SET WAREHOUSE_SIZE = XSMALL STATEMENT_TIMEOUT_IN_SECONDS = 1800 "
    "AUTO_SUSPEND = 60 AUTO_RESUME = TRUE"
)
SUSPEND = "ALTER WAREHOUSE SIZING_BENCHMARK_WH SUSPEND"


def _clock(step: float = 10.0):
    ticks = itertools.count(0, step)
    return lambda: next(ticks)


def _sweep(conn, **kwargs):
    kwargs.setdefault("sizes", ["XSMALL"])
    kwargs.setdefault("runs", 2)
    kwargs.setdefault("clock", _clock())
    kwargs.setdefault("sleep", lambda _s: None)
    return sweep.sweep_sizes(conn, objects=OBJECTS, run_id="R1", **kwargs)


# --------------------------------------------------------------------------- #
# The cost cap
# --------------------------------------------------------------------------- #
def test_query_timeouts_reserve_the_minimums_then_split_the_rest():
    # Each size may use the 60s it pays for anyway plus its share, less 10s per query for the lookups.
    timeouts = sweep.query_timeouts(["XSMALL", "MEDIUM"], generation="1", runs=1, max_credits=3.0)
    assert timeouts == {"XSMALL": 5300, "MEDIUM": 1362}


def test_the_default_cap_fits_the_articles_full_sweep():
    timeouts = sweep.query_timeouts(warehouses.SIZE_KEYWORDS, generation="1", runs=3, max_credits=3.0)
    assert (timeouts["XSMALL"], timeouts["XXLARGE"]) == (400, 22)


def test_the_default_cap_fits_the_articles_full_sweep_on_gen2():
    # Also what an account that doesn't report its generation gets.
    timeouts = sweep.query_timeouts(warehouses.SIZE_KEYWORDS, generation="2", runs=3, max_credits=3.0)
    assert timeouts["XXLARGE"] == 17


def test_query_timeouts_reject_a_cap_below_the_minimums():
    with pytest.raises(ValueError, match="doesn't cover Snowflake's 60-second minimum"):
        sweep.query_timeouts(warehouses.SIZE_KEYWORDS, generation="1", runs=3, max_credits=1.0)


def test_query_timeouts_reject_a_cap_that_is_too_tight():
    with pytest.raises(ValueError, match="only"):
        sweep.query_timeouts(warehouses.SIZE_KEYWORDS, generation="1", runs=5, max_credits=1.1)


# --------------------------------------------------------------------------- #
# The sweep
# --------------------------------------------------------------------------- #
def test_sweep_issues_the_articles_sql(account):
    cursor, conn = account()

    _sweep(conn)

    sql = cursor.executed
    assert "SET lineitem_table = 'SNOWFLAKE_SAMPLE_DATA.TPCH_SF100.LINEITEM'" in sql
    assert "USE WAREHOUSE SIZING_BENCHMARK_WH" in sql
    assert "ALTER SESSION SET USE_CACHED_RESULT = FALSE" in sql
    # The X-Small may bill 60s + (3 - 1/60 credits) x 3600, over 2 queries, less 10s each: 5390s.
    resize = "ALTER WAREHOUSE SIZING_BENCHMARK_WH SET WAREHOUSE_SIZE = XSMALL STATEMENT_TIMEOUT_IN_SECONDS = 5390"
    assert resize in sql
    assert "ALTER SESSION SET QUERY_TAG = 'wsbench:R1:XSMALL:1'" in sql
    assert "ALTER SESSION SET QUERY_TAG = 'wsbench:R1:XSMALL:2'" in sql
    assert sql.count(queries.BENCHMARK_QUERY) == 2
    assert sql[-1] == IDLE
    assert cursor.closed


def test_the_first_size_starts_suspended_and_cold(account):
    cursor, conn = account()
    _sweep(conn)
    sql = cursor.executed
    resize = next(
        i for i, s in enumerate(sql) if "SET WAREHOUSE_SIZE = XSMALL STATEMENT_TIMEOUT_IN_SECONDS = 5390" in s
    )
    assert SUSPEND in sql[:resize]


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
    assert result.query_credits == round(42.0 * 1.35 / 3600, 5)  # the median run
    assert result.billed_credits == round(84.0 * 1.35 / 3600, 5)  # both runs, back to back


def test_query_credits_use_the_median_run(account):
    result = sweep.SizeResult("XSMALL", "X-Small", 1.0, (100.0, 40.0, 50.0), 1.0, 0.0)
    assert result.query_credits == round(50.0 / 3600, 5)


def test_billed_credits_count_the_whole_time_the_size_was_up(account):
    result = sweep.SizeResult("XSMALL", "X-Small", 1.0, (100.0,), 1.0, 0.0, billed_s=130.0)
    assert result.billed_credits == round(130.0 / 3600, 5)


def test_short_runs_bill_the_60_second_minimum(account):
    _cursor, conn = account(elapsed_ms=9_000)
    [result] = _sweep(conn, sizes=["MEDIUM"], runs=1)
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


def test_a_capped_cold_run_is_reported_skips_warm_runs_and_still_reads_spill(account, snowflake_timeout):
    cursor, conn = account(fail={queries.BENCHMARK_QUERY: [snowflake_timeout()]})
    messages: list[str] = []

    [result] = _sweep(conn, runs=3, echo=messages.append)

    assert result.capped_runs == (1,) and result.cold_capped and len(result.runtimes_s) == 1
    assert result.gb_spill_local == 3.0
    assert cursor.executed.count(queries.BENCHMARK_QUERY) == 1
    assert any("stopped by the cost cap" in m for m in messages)


def test_a_capped_warm_run_leaves_the_cold_run_exact(account, snowflake_timeout):
    # The first run finishes; the second is cancelled.
    _cursor, conn = account(fail={queries.BENCHMARK_QUERY: [None, snowflake_timeout()]})

    [result] = _sweep(conn, runs=3)

    assert result.capped_runs == (2,)
    assert not result.cold_capped and result.warm_capped


def test_a_failed_spill_lookup_says_why_and_keeps_the_run(account):
    _cursor, conn = account(fail={"GET_QUERY_OPERATOR_STATS": [RuntimeError("insufficient privileges")]})
    messages: list[str] = []

    [result] = _sweep(conn, runs=1, echo=messages.append)

    assert result.gb_spill_local is None and result.runtimes_s == (42.0,)
    assert any("couldn't read spill: insufficient privileges" in m for m in messages)


def test_other_errors_propagate_after_suspending_and_resetting(account):
    # The query fails, and so does every SUSPEND; the query's error must be the one that surfaces.
    cursor, conn = account(fail={queries.BENCHMARK_QUERY: [RuntimeError("boom")], SUSPEND: [RuntimeError("x")] * 5})

    with pytest.raises(RuntimeError, match="boom"):
        _sweep(conn)
    assert cursor.executed[-1] == IDLE


def test_sweep_needs_setup(account):
    cursor, conn = account(warehouse_comment=None)
    with pytest.raises(ValueError, match="warehouse-sizing setup"):
        _sweep(conn)
    assert not any(s.startswith("ALTER WAREHOUSE") for s in cursor.executed)


def test_sweep_refuses_someone_elses_warehouse(account):
    cursor, conn = account(warehouse_comment="production")
    with pytest.raises(ValueError, match="this experiment didn't create it"):
        _sweep(conn)
    assert not any(s.startswith("ALTER WAREHOUSE") for s in cursor.executed)


def test_the_cap_must_cover_the_minimums(account):
    _cursor, conn = account()
    with pytest.raises(ValueError, match="60-second minimum"):
        _sweep(conn, sizes=warehouses.SIZE_KEYWORDS, runs=3, max_credits=0.5)


@pytest.mark.parametrize("bad", ["bad'id", "x y"])
def test_run_id_must_be_tag_safe(account, bad):
    _cursor, conn = account()
    with pytest.raises(ValueError, match="run id"):
        sweep.sweep_sizes(conn, objects=OBJECTS, run_id=bad)
