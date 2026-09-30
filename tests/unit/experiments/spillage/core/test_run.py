"""Unit tests for running the comparison."""

from __future__ import annotations

import itertools

import pytest

from experiments.spillage.core import infra, queries, run

GB = 1024**3
OBJECTS = infra.DemoObjects.named()
LOCAL = queries.SCENARIOS["local"]
REMOTE = queries.SCENARIOS["remote"]
WORKLOAD = queries.build_workload(OBJECTS.table, LOCAL.fanout)


def _clock(step: float = 10.0):
    """A fake perf_counter that advances ``step`` seconds per call."""
    ticks = itertools.count(0, step)
    return lambda: next(ticks)


def _no_sleep(_seconds: float) -> None:
    pass


def _run(conn, scenario=LOCAL, **kwargs):
    kwargs.setdefault("clock", _clock())
    kwargs.setdefault("sleep", _no_sleep)
    return run.run_comparison(conn, objects=OBJECTS, scenario=scenario, run_id="R1", **kwargs)


class _SnowflakeTimeout(Exception):
    errno = 630


def _fail_workload(cursor, error, *, times=1):
    """Make the first ``times`` executions of the local workload raise ``error``."""
    real_execute = cursor.execute
    remaining = {"n": times}

    def execute(sql, *args):
        real_execute(sql, *args)
        if sql == WORKLOAD and remaining["n"] > 0:
            remaining["n"] -= 1
            raise error
        return cursor

    cursor.execute = execute


def test_run_issues_expected_sql(account):
    cursor, conn = account()

    _run(conn)

    sql = cursor.executed
    assert "USE WAREHOUSE SPILLAGE_DEMO_WH" in sql
    assert "ALTER SESSION SET USE_CACHED_RESULT = FALSE" in sql
    # Undersized first, each with its own half of the 1.5-credit cap.
    xsmall = "ALTER WAREHOUSE SPILLAGE_DEMO_WH SET WAREHOUSE_SIZE = XSMALL STATEMENT_TIMEOUT_IN_SECONDS = 2700"
    medium = "ALTER WAREHOUSE SPILLAGE_DEMO_WH SET WAREHOUSE_SIZE = MEDIUM STATEMENT_TIMEOUT_IN_SECONDS = 675"
    assert sql.index(xsmall) < sql.index(medium)
    assert "ALTER SESSION SET QUERY_TAG = 'spill:R1:local:undersized'" in sql
    assert "ALTER SESSION SET QUERY_TAG = 'spill:R1:local:right_sized'" in sql
    assert sql.count(WORKLOAD) == 2  # identical query on both sizes
    assert sql.count("ALTER WAREHOUSE SPILLAGE_DEMO_WH SUSPEND") == 2
    # Left at the cheapest size, with setup's timeout rather than the Medium's, for `report`.
    assert sql[-1] == "ALTER WAREHOUSE SPILLAGE_DEMO_WH SET WAREHOUSE_SIZE = XSMALL STATEMENT_TIMEOUT_IN_SECONDS = 900"
    assert cursor.closed


def test_query_tag_is_cleared_before_the_stats_lookup(account):
    cursor, conn = account()

    _run(conn)

    sql = cursor.executed
    workload_at = sql.index(WORKLOAD)
    stats_at = next(i for i, s in enumerate(sql) if "QUERY_HISTORY_BY_SESSION" in s)
    assert "ALTER SESSION UNSET QUERY_TAG" in sql[workload_at:stats_at]
    assert "SPILLAGE_DEMO_DB.INFORMATION_SCHEMA" in sql[stats_at]


def test_run_needs_setup_first(account):
    cursor, conn = account(warehouse_comment=None)
    with pytest.raises(ValueError, match="spillage setup"):
        _run(conn)
    assert not any("WAREHOUSE_SIZE" in s for s in cursor.executed)


def test_run_refuses_someone_elses_warehouse(account):
    cursor, conn = account(warehouse_comment="production")
    with pytest.raises(ValueError, match="wasn't created by this experiment"):
        _run(conn)
    assert not any(s.startswith("ALTER WAREHOUSE") for s in cursor.executed)


def test_results_use_snowflake_time_and_the_generation_rate(account):
    _cursor, conn = account(generation="2", live_stats=[(3 * GB, GB // 2, 42_000)])

    undersized, right_sized = _run(conn)

    assert (undersized.side, right_sized.side) == ("undersized", "right_sized")
    assert (undersized.size_label, right_sized.size_label) == ("X-Small", "Medium")
    assert undersized.runtime_s == 42.0  # Snowflake's elapsed, not the fake clock's 10s
    assert (undersized.gb_spill_local, undersized.gb_spill_remote) == (3.0, 0.5)
    assert undersized.credits_per_hour == pytest.approx(1.35)
    assert right_sized.est_credits == round(42.0 * 4 * 1.35 / 3600, 5)


def test_run_falls_back_to_client_time_when_stats_never_arrive(account):
    _cursor, conn = account(live_stats=[])
    messages: list[str] = []

    undersized, _right_sized = _run(conn, clock=_clock(5.0), echo=messages.append)

    assert undersized.gb_spill_local is None
    assert undersized.runtime_s == 5.0
    assert any("live stats not available yet" in m for m in messages)


def test_stats_lookup_retries_until_the_row_arrives(account):
    cursor, conn = account(live_stats_sequence=[[], [], [(GB, 0, 1_000)]])

    undersized, _right_sized = _run(conn)

    assert undersized.gb_spill_local == 1.0
    # Two misses and a hit for the first side, then one hit for the second.
    assert sum("QUERY_HISTORY_BY_SESSION" in s for s in cursor.executed) == 4


def test_a_failed_stats_lookup_keeps_the_run(account):
    cursor, conn = account()
    real_execute = cursor.execute

    def execute(sql, *args):
        real_execute(sql, *args)
        if "QUERY_HISTORY_BY_SESSION" in sql:
            raise RuntimeError("no current database")
        return cursor

    cursor.execute = execute
    messages: list[str] = []

    results = _run(conn, echo=messages.append)

    assert len(results) == 2 and results[0].gb_spill_local is None
    assert any("couldn't read live stats" in m for m in messages)


def test_cost_cap_timeout_is_reported_not_fatal(account):
    cursor, conn = account(live_stats=[(20 * GB, 0, 2_700_000)])
    _fail_workload(cursor, _SnowflakeTimeout("Statement reached its statement or warehouse timeout"))
    messages: list[str] = []

    undersized, right_sized = _run(conn, echo=messages.append)

    assert undersized.timed_out and not right_sized.timed_out
    assert any("stopped by the cost cap" in m for m in messages)
    assert any("hit the cost cap" in m for m in messages)  # the calibration hint
    assert cursor.executed.count("ALTER WAREHOUSE SPILLAGE_DEMO_WH SUSPEND") == 2


def test_other_errors_propagate_after_suspending_and_resetting(account):
    cursor, conn = account()
    _fail_workload(cursor, RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        _run(conn)
    assert cursor.executed[-2:] == [
        "ALTER WAREHOUSE SPILLAGE_DEMO_WH SUSPEND",
        "ALTER WAREHOUSE SPILLAGE_DEMO_WH SET WAREHOUSE_SIZE = XSMALL STATEMENT_TIMEOUT_IN_SECONDS = 900",
    ]


def test_a_failed_suspend_does_not_hide_the_real_error(account):
    cursor, conn = account()
    real_execute = cursor.execute

    def execute(sql, *args):
        real_execute(sql, *args)
        if sql == WORKLOAD:
            raise RuntimeError("resource monitor suspended the warehouse")
        if sql.endswith(" SUSPEND"):
            raise RuntimeError("Invalid state. Warehouse cannot be suspended.")
        return cursor

    cursor.execute = execute

    with pytest.raises(RuntimeError, match="resource monitor"):
        _run(conn)


def test_run_id_must_be_tag_safe(account):
    _cursor, conn = account()
    with pytest.raises(ValueError, match="run id"):
        run.run_comparison(conn, objects=OBJECTS, scenario=LOCAL, run_id="bad'id")


def test_unknown_size_is_rejected_before_anything_runs(account):
    cursor, conn = account()
    with pytest.raises(ValueError, match="size must be one of"):
        _run(conn, queries.resolve_scenario("local", right_sized="HUGE"))
    assert not any(s.startswith("ALTER WAREHOUSE") for s in cursor.executed)


def test_progress_names_the_run_and_the_cap(account):
    _cursor, conn = account()
    messages: list[str] = []

    _run(conn, REMOTE, echo=messages.append)

    joined = "\n".join(messages)
    assert "Local + remote spill" in joined
    assert "Run id: R1" in joined
    assert "Cost cap: at most 1.5 credits of Gen1 compute (X-Small stops after 45 min" in joined
    assert "=== undersized: X-Small ===" in joined
    assert "=== right-sized: Medium ===" in joined


def _side(side, local, remote, *, timed_out=False):
    return run.SideResult(side, "X-Small", 1.0, 1.0, local, remote, 0.1, timed_out)


def _pair(u_local, u_remote, r_local, r_remote, *, undersized_timed_out=False):
    return [
        _side("undersized", u_local, u_remote, timed_out=undersized_timed_out),
        _side("right_sized", r_local, r_remote),
    ]


def test_hints_are_silent_when_the_scenario_hits_its_target():
    assert run.calibration_hints(LOCAL, _pair(5.0, 0.0, 0.0, 0.0)) == []
    assert run.calibration_hints(REMOTE, _pair(50.0, 20.0, 30.0, 0.0)) == []


def test_hints_say_raise_fanout_when_undersized_does_not_spill():
    [hint] = run.calibration_hints(LOCAL, _pair(0.0, 0.0, 0.0, 0.0))
    assert "higher --fanout (e.g. 16)" in hint


def test_hints_say_lower_fanout_when_right_sized_also_spills():
    [hint] = run.calibration_hints(LOCAL, _pair(9.0, 0.0, 2.0, 0.0))
    assert "lower --fanout (e.g. 4)" in hint


def test_remote_hint_when_no_remote_spill():
    [hint] = run.calibration_hints(REMOTE, _pair(30.0, 0.0, 0.0, 0.0))
    assert "remote spill" in hint and "(e.g. 80)" in hint


def test_timeout_hint_replaces_spill_hints():
    [hint] = run.calibration_hints(LOCAL, _pair(0.0, 0.0, 0.0, 0.0, undersized_timed_out=True))
    assert "raise --max-credits" in hint


def test_hints_skip_when_live_stats_are_missing():
    assert run.calibration_hints(LOCAL, _pair(None, None, None, None)) == []
