"""Unit tests for the spillage run / teardown domain layer."""

from __future__ import annotations

import itertools

import pytest

from experiments.spillage.core import queries, run

LIVE_DESCRIPTION = [
    ("BYTES_LOCAL",),
    ("BYTES_REMOTE",),
    ("PARTITIONS_SCANNED",),
    ("PARTITIONS_TOTAL",),
    ("TOTAL_ELAPSED_TIME",),
]
GB = 1024**3


def _clock(step: float = 10.0):
    """A fake perf_counter that advances ``step`` seconds per call."""
    ticks = itertools.count(0, step)
    return lambda: next(ticks)


def _no_sleep(_seconds: float) -> None:
    pass


def test_run_comparison_issues_expected_sql(make_cursor, make_connection):
    cursor = make_cursor()
    conn = make_connection(cursor)

    run.run_comparison(conn, scenario=queries.SCENARIOS["local"], clock=_clock(), sleep=_no_sleep)

    sql = cursor.executed
    assert "SHOW TERSE OBJECTS LIKE 'LINEITEM' IN SCHEMA SNOWFLAKE_SAMPLE_DATA.TPCH_SF10" in sql
    assert "SET spill_table = 'SNOWFLAKE_SAMPLE_DATA.TPCH_SF10.LINEITEM'" in sql
    assert any("CREATE WAREHOUSE IF NOT EXISTS SPILLAGE_DEMO_WH" in s for s in sql)
    assert "ALTER SESSION SET USE_CACHED_RESULT = FALSE" in sql
    assert "ALTER WAREHOUSE SPILLAGE_DEMO_WH SET WAREHOUSE_SIZE = XSMALL" in sql
    assert "ALTER WAREHOUSE SPILLAGE_DEMO_WH SET WAREHOUSE_SIZE = MEDIUM" in sql
    assert "ALTER SESSION SET QUERY_TAG = 'spill:local:undersized:1'" in sql
    assert "ALTER SESSION SET QUERY_TAG = 'spill:local:right_sized:1'" in sql
    assert sql.count(queries.build_workload(8)) == 2  # identical query, once per arm
    assert sql.count(queries.LIVE_STATS_SQL) == 2
    assert sql.count("ALTER WAREHOUSE SPILLAGE_DEMO_WH SUSPEND") == 2
    # The 1.5-credit default cap: 0.75 credits each -> 45 min on X-Small, 11 min on Medium.
    assert "ALTER WAREHOUSE SPILLAGE_DEMO_WH SET STATEMENT_TIMEOUT_IN_SECONDS = 2700" in sql
    assert "ALTER WAREHOUSE SPILLAGE_DEMO_WH SET STATEMENT_TIMEOUT_IN_SECONDS = 675" in sql
    # Undersized runs before right-sized.
    assert sql.index("ALTER WAREHOUSE SPILLAGE_DEMO_WH SET WAREHOUSE_SIZE = XSMALL") < sql.index(
        "ALTER WAREHOUSE SPILLAGE_DEMO_WH SET WAREHOUSE_SIZE = MEDIUM"
    )
    assert cursor.closed


def test_run_comparison_reads_live_spill_stats(make_cursor, make_connection):
    # Every statement returns this row; the live-stats query is the one that parses it.
    cursor = make_cursor(description=LIVE_DESCRIPTION, fetch=[(3 * GB, GB // 2, 100, 100, 42_000)])
    conn = make_connection(cursor)

    results = run.run_comparison(conn, scenario=queries.SCENARIOS["local"], clock=_clock(), sleep=_no_sleep)

    assert [r.arm for r in results] == ["undersized", "right_sized"]
    undersized, right_sized = results
    assert undersized.size_label == "X-Small"
    assert right_sized.size_label == "Medium"
    assert undersized.gb_spill_local == 3.0
    assert undersized.gb_spill_remote == 0.5
    assert undersized.elapsed_s == 42.0
    # est credits use Snowflake's elapsed time x the size's credits/hr.
    assert undersized.est_credits == round(42.0 * 1 / 3600, 5)
    assert right_sized.est_credits == round(42.0 * 4 / 3600, 5)


def test_run_comparison_falls_back_to_client_timing(make_cursor, make_connection):
    cursor = make_cursor(description=LIVE_DESCRIPTION, fetch=[])  # no live stats, ever
    conn = make_connection(cursor)
    messages: list[str] = []

    # fetch=[] would fail the sample-data check, so use a non-sample table.
    scenario = queries.resolve_scenario("local", table="MYDB.MYSCHEMA.BIG_TABLE")
    results = run.run_comparison(conn, scenario=scenario, echo=messages.append, clock=_clock(5.0), sleep=_no_sleep)

    assert results[0].gb_spill_local is None
    assert results[0].runtime_s == 5.0
    assert results[0].est_credits == round(5.0 * 1 / 3600, 5)
    assert any("live stats not available" in m for m in messages)


def test_run_comparison_reports_progress(make_cursor, make_connection):
    conn = make_connection(make_cursor())
    messages: list[str] = []

    run.run_comparison(
        conn, scenario=queries.SCENARIOS["remote"], echo=messages.append, clock=_clock(), sleep=_no_sleep
    )

    joined = "\n".join(messages)
    assert "Local + remote spill" in joined
    assert "undersized: X-Small (XSMALL)" in joined
    assert "right-sized: Medium (MEDIUM)" in joined
    assert "Cost cap: at most 1.5 credits" in joined
    assert "run 1 (cold)" in joined


def test_remote_scenario_hints_when_no_remote_spill(make_cursor, make_connection):
    cursor = make_cursor(description=LIVE_DESCRIPTION, fetch=[(3 * GB, 0, 100, 100, 42_000)])
    messages: list[str] = []

    run.run_comparison(
        make_connection(cursor),
        scenario=queries.SCENARIOS["remote"],
        echo=messages.append,
        clock=_clock(),
        sleep=_no_sleep,
    )

    assert any("higher --fanout (e.g. 80)" in m for m in messages)


def _results(under_local, under_remote, right_local, right_remote, *, under_timed_out=False):
    def arm(name, local, remote, timed_out=False):
        return run.ArmResult(name, "XSMALL", "X-Small", 1, 1.0, 1.0, local, remote, 1, 1, 0.1, "q", timed_out)

    return [
        arm("undersized", under_local, under_remote, under_timed_out),
        arm("right_sized", right_local, right_remote),
    ]


class _SnowflakeTimeout(Exception):
    errno = 630


def _time_out_first_workload(cursor, workload):
    """Make the first execution of ``workload`` fail like a statement timeout."""
    real_execute = cursor.execute
    state = {"raised": False}

    def execute(sql, *args):
        real_execute(sql, *args)
        if sql == workload and not state["raised"]:
            state["raised"] = True
            raise _SnowflakeTimeout("Statement reached its statement or warehouse timeout")
        return cursor

    cursor.execute = execute


def test_cost_cap_timeout_is_reported_not_fatal(make_cursor, make_connection):
    cursor = make_cursor(description=LIVE_DESCRIPTION, fetch=[(20 * GB, 0, 100, 100, 2_700_000)])
    _time_out_first_workload(cursor, queries.build_workload(8))
    messages: list[str] = []

    results = run.run_comparison(
        make_connection(cursor),
        scenario=queries.SCENARIOS["local"],
        runs=2,
        echo=messages.append,
        clock=_clock(),
        sleep=_no_sleep,
    )

    undersized, right_sized = results
    assert undersized.timed_out and not right_sized.timed_out
    joined = "\n".join(messages)
    assert "stopped by the cost cap after 1350s" in joined  # runs=2 halves each run's share
    assert joined.count("run 2") == 1  # the capped arm skips its repeat; Medium still repeats
    assert cursor.executed.count("ALTER WAREHOUSE SPILLAGE_DEMO_WH SUSPEND") == 2
    assert any("hit the cost cap" in m for m in messages)


def test_other_errors_still_suspend_the_warehouse(make_cursor, make_connection):
    cursor = make_cursor()
    real_execute = cursor.execute
    workload = queries.build_workload(8)

    def execute(sql, *args):
        real_execute(sql, *args)
        if sql == workload:
            raise RuntimeError("boom")
        return cursor

    cursor.execute = execute

    with pytest.raises(RuntimeError, match="boom"):
        run.run_comparison(make_connection(cursor), scenario=queries.SCENARIOS["local"], sleep=_no_sleep)
    assert cursor.executed[-1] == "ALTER WAREHOUSE SPILLAGE_DEMO_WH SUSPEND"


def test_timeout_hint_replaces_spill_hints():
    hints = run.calibration_hints(queries.SCENARIOS["local"], _results(0.0, 0.0, 0.0, 0.0, under_timed_out=True))
    assert len(hints) == 1
    assert "raise --max-credits" in hints[0]


def test_hints_are_silent_when_the_scenario_hits_its_target():
    local = queries.SCENARIOS["local"]
    remote = queries.SCENARIOS["remote"]
    assert run.calibration_hints(local, _results(5.0, 0.0, 0.0, 0.0)) == []
    assert run.calibration_hints(remote, _results(50.0, 20.0, 30.0, 0.0)) == []


def test_hints_say_raise_fanout_when_undersized_does_not_spill():
    [hint] = run.calibration_hints(queries.SCENARIOS["local"], _results(0.0, 0.0, 0.0, 0.0))
    assert "higher --fanout (e.g. 16)" in hint


def test_hints_say_lower_fanout_when_right_sized_also_spills():
    [hint] = run.calibration_hints(queries.SCENARIOS["local"], _results(9.0, 0.0, 2.0, 0.0))
    assert "lower --fanout (e.g. 4)" in hint


def test_hints_skip_when_live_stats_are_missing():
    assert run.calibration_hints(queries.SCENARIOS["local"], _results(None, None, None, None)) == []


def test_run_comparison_missing_sample_data_raises(make_cursor, make_connection):
    conn = make_connection(make_cursor(fetch=[]))  # SHOW returns nothing

    with pytest.raises(ValueError, match="not found"):
        run.run_comparison(conn, scenario=queries.SCENARIOS["local"], sleep=_no_sleep)


def test_run_comparison_rejects_bad_identifier(make_cursor, make_connection):
    conn = make_connection(make_cursor())
    scenario = queries.resolve_scenario("local", table="bad; DROP TABLE x")

    with pytest.raises(ValueError, match="table"):
        run.run_comparison(conn, scenario=scenario)


def test_run_comparison_rejects_unknown_size(make_cursor, make_connection):
    conn = make_connection(make_cursor())
    scenario = queries.resolve_scenario("local", right_sized="HUGE")

    with pytest.raises(ValueError, match="right_sized size"):
        run.run_comparison(conn, scenario=scenario)


def test_drop_warehouse(make_cursor, make_connection):
    cursor = make_cursor()
    messages: list[str] = []

    run.drop_warehouse(make_connection(cursor), warehouse_name="MY_WH", echo=messages.append)

    assert "DROP WAREHOUSE IF EXISTS MY_WH" in cursor.executed
    assert messages == ["Dropped MY_WH."]
