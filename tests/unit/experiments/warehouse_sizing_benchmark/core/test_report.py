"""Unit tests for the results tables and the ACCOUNT_USAGE report."""

from __future__ import annotations

import pytest

from experiments.warehouse_sizing_benchmark.core import infra, queries, report
from experiments.warehouse_sizing_benchmark.core.sweep import SizeResult

OBJECTS = infra.BenchmarkObjects.named()


def _size(keyword, label, rate, runtimes, local, remote=0.0, *, capped=(), up_s=None):
    return SizeResult(keyword, label, rate, tuple(runtimes), local, remote, tuple(capped), up_s)


# The live comparison on TPCH_SF100: X-Small spilled, Medium spilled a little.
XSMALL = _size("XSMALL", "X-Small", 1.0, [117.2], 22.82)
MEDIUM = _size("MEDIUM", "Medium", 4.0, [15.4], 1.54)


def _changes(table):
    return {row[0]: row[3] for row in table.rows}


def test_the_results_table_has_plain_headers_and_hides_warm_for_single_runs():
    [table] = report.live_tables([XSMALL, MEDIUM, _size("LARGE", "Large", 8.0, [5.0], 0.0)])
    assert table.step is None
    assert table.columns == [
        "size", "credits/hr", "runs", "cold (s)",
        "local spill (GB)", "remote spill (GB)", "credits/query", "credits billed",
    ]  # fmt: skip
    assert [row[0] for row in table.rows] == ["X-Small", "Medium", "Large"]


def test_the_warm_column_shows_up_with_more_runs():
    [table] = report.live_tables([_size("LARGE", "Large", 8.0, [5.0, 4.0, 4.2], 0.0), XSMALL, MEDIUM])
    assert "warm (s)" in table.columns
    assert table.rows[0][4] == "4.1"  # median of the warm runs


def test_two_sizes_go_side_by_side_smaller_first():
    _table, side_by_side = report.live_tables([MEDIUM, XSMALL])
    assert side_by_side.title == "X-Small vs Medium"
    assert side_by_side.columns == ["metric", "X-Small", "Medium", "change"]
    assert _changes(side_by_side) == {
        "runtime, cold (s)": "7.6x faster",
        "local spill (GB)": "93% less",
        "remote spill (GB)": "none",
        "credits per query": "47% cheaper",
        # The Medium's 15s rounds up to the 60-second minimum on this run's bill.
        "credits billed for this run": "2.0x as much",
    }


def test_big_cost_changes_read_as_ratios_and_small_ones_as_percentages():
    assert report._cost_change(0.02, 0.06) == "3.0x as much"
    assert report._cost_change(0.02, 0.03) == "50% more"
    assert report._cost_change(0.02, 0.01) == "50% cheaper"


def test_a_capped_cold_run_is_a_lower_bound():
    capped = _size("XSMALL", "X-Small", 1.0, [2700.0], 30.0, capped=[1])
    table, side_by_side = report.live_tables([capped, MEDIUM])
    assert table.rows[0][3] == "2700.0+"
    assert table.rows[0][4] == "30.00+"
    assert _changes(side_by_side)["runtime, cold (s)"] == "at least 175.3x faster"
    assert _changes(side_by_side)["credits per query"] == "—"


def test_a_capped_warm_run_leaves_the_cold_numbers_exact():
    warm_capped = _size("XSMALL", "X-Small", 1.0, [70.0, 300.0], 16.0, capped=[2])
    [table] = report.live_tables([warm_capped])
    assert table.rows[0][3] == "70.0"  # cold
    assert table.rows[0][4] == "300.0+"  # warm
    assert table.rows[0][5] == "16.00"  # spill comes from the cold run


def test_summary_totals_credits_and_names_the_cheapest():
    lines = report.summary_lines([XSMALL, MEDIUM])
    assert lines[0].startswith("This run used about 0.099 credits")  # 0.03256 + 0.06667
    assert "Step 15 of `warehouse-sizing report`" in lines[0]
    assert lines[1] == "Cheapest per query: Medium (0.01711 credits)."


def test_summary_explains_the_60_second_minimum():
    lines = report.summary_lines([XSMALL, MEDIUM])
    assert any(line.startswith("The Medium ran for 15s but bills the 60-second minimum.") for line in lines)
    assert not any("The X-Small ran for" in line for line in lines)


def test_a_little_residual_spill_is_not_flagged():
    assert not any("spilled too" in line for line in report.summary_lines([XSMALL, MEDIUM]))


def test_the_larger_size_spilling_a_real_share_is_flagged():
    also_spilled = _size("MEDIUM", "Medium", 4.0, [20.0], 10.0)
    assert any("spilled too" in line for line in report.summary_lines([XSMALL, also_spilled]))


def test_no_spill_hint_when_the_smaller_spill_is_unknown():
    unknown = _size("XSMALL", "X-Small", 1.0, [117.2], None)
    assert not any("spilled too" in line for line in report.summary_lines([unknown, MEDIUM]))


def test_the_no_spill_hint_fits_the_smaller_size():
    no_spill = _size("XSMALL", "X-Small", 1.0, [30.0], 0.0)
    [hint] = [line for line in report.summary_lines([no_spill, MEDIUM]) if "didn't spill" in line]
    assert "smaller first --size" not in hint  # there's nothing smaller than an X-Small
    assert "TPCH_SF1000" in hint and "--max-credits" in hint

    small = _size("SMALL", "Small", 2.0, [30.0], 0.0)
    [hint] = [line for line in report.summary_lines([small, MEDIUM]) if "didn't spill" in line]
    assert "smaller first --size" in hint


def test_read_report_reads_the_latest_run_on_the_benchmark_warehouse(account):
    cursor, conn = account(latest_run="20260930-120000")

    run_id, tables = report.read_report(conn, objects=OBJECTS, hours=12)

    assert run_id == "20260930-120000"
    assert [t.step for t in tables] == [step for step, _, _ in queries.REPORT_STEPS]
    sql = cursor.executed
    use = sql.index("USE WAREHOUSE SIZING_BENCHMARK_WH")
    steps = [s for s in sql if "ACCOUNT_USAGE" in s and "MAX(SPLIT_PART" not in s]
    assert len(steps) == len(queries.REPORT_STEPS) and sql.index(steps[0]) > use
    assert all("warehouse_name = 'SIZING_BENCHMARK_WH'" in s for s in steps)
    assert sum("query_tag LIKE 'wsbench:20260930-120000:%'" in s for s in steps) == len(queries.REPORT_STEPS) - 1
    assert sql[-1] == "ALTER WAREHOUSE SIZING_BENCHMARK_WH SUSPEND"  # doesn't wait for AUTO_SUSPEND
    assert cursor.closed


def test_read_report_can_pick_a_run(account):
    cursor, conn = account()
    run_id, _tables = report.read_report(conn, objects=OBJECTS, run_id="R7")
    assert run_id == "R7"
    assert not any("MAX(SPLIT_PART" in s for s in cursor.executed)


def test_read_report_with_no_runs_yet(account):
    _cursor, conn = account(latest_run=None)
    assert report.read_report(conn, objects=OBJECTS) == (None, [])


def test_read_report_prices_gen2_and_needs_only_the_warehouse(account):
    # The report reads ACCOUNT_USAGE, so a missing database or unreadable share doesn't matter.
    cursor, conn = account(generation="2", database_comment=None, sample_data=False)

    _run_id, tables = report.read_report(conn, objects=OBJECTS)

    assert len(tables) == len(queries.REPORT_STEPS)
    assert any("rate.cph * 1.35" in s for s in cursor.executed)


def test_read_report_needs_the_warehouse(account):
    _cursor, conn = account(warehouse_comment=None)
    with pytest.raises(ValueError, match="warehouse-sizing setup"):
        report.read_report(conn, objects=OBJECTS)


@pytest.mark.parametrize(("kwargs", "match"), [({"hours": 0}, "hours"), ({"run_id": "bad'id"}, "run id")])
def test_read_report_rejects_unsafe_values(account, kwargs, match):
    _cursor, conn = account()
    with pytest.raises(ValueError, match=match):
        report.read_report(conn, objects=OBJECTS, **kwargs)
