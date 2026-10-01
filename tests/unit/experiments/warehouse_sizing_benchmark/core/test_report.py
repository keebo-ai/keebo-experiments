"""Unit tests for the live results and the ACCOUNT_USAGE report."""

from __future__ import annotations

import pytest

from experiments.warehouse_sizing_benchmark.core import infra, queries, report
from experiments.warehouse_sizing_benchmark.core.sweep import SizeResult

OBJECTS = infra.BenchmarkObjects.named()


def _size(keyword, label, rate, runtimes, local, remote=0.0, *, timed_out=False):
    return SizeResult(keyword, label, rate, tuple(runtimes), local, remote, timed_out)


# The live `local` comparison: X-Small spilled, Medium didn't.
XSMALL = _size("XSMALL", "X-Small", 1.0, [74.0], 15.13)
MEDIUM = _size("MEDIUM", "Medium", 4.0, [9.0], 0.0)


def _changes(side_by_side):
    return {row[0]: row[3] for row in side_by_side.rows}


def test_live_table_lists_every_size():
    [table] = report.live_tables([XSMALL, MEDIUM, _size("LARGE", "Large", 8.0, [5.0, 4.0, 4.2], 0.0)])
    assert table.step is None
    assert table.columns[0] == "size" and table.columns[-2:] == ["query_credits", "billed_credits"]
    assert [row[0] for row in table.rows] == ["X-Small", "Medium", "Large"]
    assert table.rows[2][4] == "4.1"  # warm median


def test_two_sizes_go_side_by_side_smaller_first():
    _table, side_by_side = report.live_tables([MEDIUM, XSMALL])
    assert side_by_side.title == "X-Small vs Medium"
    assert side_by_side.columns == ["metric", "X-Small", "Medium", "change"]
    assert _changes(side_by_side) == {
        "runtime, cold (s)": "8.2x faster",
        "local spill (GB)": "eliminated",
        "remote spill (GB)": "—",
        "credits per query": "51% cheaper",
        # The Medium's 9s rounds up to Snowflake's 60-second minimum on this run's bill.
        "credits billed for this run": "224% pricier",
    }


def test_a_capped_size_is_a_lower_bound():
    capped = _size("XSMALL", "X-Small", 1.0, [2700.0], 30.0, timed_out=True)
    table, side_by_side = report.live_tables([capped, MEDIUM])
    assert table.rows[0][3] == "2700.0+"
    assert _changes(side_by_side)["runtime, cold (s)"] == "at least 300.0x faster"
    assert _changes(side_by_side)["credits per query"] == "—"


def test_summary_totals_credits_and_names_the_cheapest():
    lines = report.summary_lines([XSMALL, MEDIUM])
    assert lines[0].startswith("This run used about 0.087 credits.")  # 0.02056 + 0.06667
    assert lines[1] == "Cheapest per query: Medium (0.01000 credits)."


def test_a_little_residual_spill_is_not_flagged():
    # The live SF100 comparison: 22.8 GB on the X-Small, 1.5 GB on the Medium. Still a clear win.
    lines = report.summary_lines(
        [_size("XSMALL", "X-Small", 1.0, [111.4], 22.82), _size("MEDIUM", "Medium", 4.0, [14.2], 1.54)]
    )
    assert not any("spilled too" in line for line in lines)


def test_summary_hints_when_the_comparison_misses():
    no_spill = _size("XSMALL", "X-Small", 1.0, [30.0], 0.0)
    also_spilled = _size("MEDIUM", "Medium", 4.0, [20.0], 5.0)
    lines = report.summary_lines([no_spill, also_spilled])
    assert any("didn't spill" in line and "TPCH_SF1000" in line for line in lines)
    assert any("spilled too" in line for line in lines)


def test_read_report_reads_the_latest_run_on_the_benchmark_warehouse(account):
    cursor, conn = account(latest_run="20260930-120000")

    run_id, tables = report.read_report(conn, objects=OBJECTS, hours=12)

    assert run_id == "20260930-120000"
    assert [t.step for t in tables] == [step for step, _, _ in queries.REPORT_STEPS]
    sql = cursor.executed
    use = sql.index("USE WAREHOUSE SIZING_BENCHMARK_WH")
    steps = [s for s in sql if "ACCOUNT_USAGE" in s and "MAX(SPLIT_PART" not in s]
    assert len(steps) == 7 and sql.index(steps[0]) > use
    assert all("warehouse_name = 'SIZING_BENCHMARK_WH'" in s for s in steps)
    assert sum("query_tag LIKE 'wsbench:20260930-120000:%'" in s for s in steps) == 6
    assert all("-12," in s for s in steps)
    assert cursor.closed


def test_read_report_can_pick_a_run(account):
    cursor, conn = account()
    run_id, _tables = report.read_report(conn, objects=OBJECTS, run_id="R7")
    assert run_id == "R7"
    assert not any("MAX(SPLIT_PART" in s for s in cursor.executed)


def test_read_report_with_no_runs_yet(account):
    _cursor, conn = account(latest_run=None)
    assert report.read_report(conn, objects=OBJECTS) == (None, [])


def test_read_report_prices_gen2_and_needs_no_readable_table(account):
    # The report only reads ACCOUNT_USAGE, so an unreadable share doesn't matter.
    cursor, conn = account(generation="2", sample_data=False, generated_table=False)

    _run_id, tables = report.read_report(conn, objects=OBJECTS)

    assert len(tables) == 7
    assert any("rate.cph * 1.35" in s for s in cursor.executed)


def test_read_report_needs_setup(account):
    _cursor, conn = account(warehouse_comment=None)
    with pytest.raises(ValueError, match="warehouse-sizing setup"):
        report.read_report(conn, objects=OBJECTS)
