"""Unit tests for the spillage report layer."""

from __future__ import annotations

import pytest

from experiments.spillage.core import infra, queries, report
from experiments.spillage.core.run import SideResult

LOCAL = queries.SCENARIOS["local"]


def _side(side, label, rate, runtime, local, remote, credits, *, timed_out=False):
    return SideResult(side, label, rate, runtime, local, remote, credits, timed_out)


RESULTS = [
    _side("undersized", "X-Small", 1.0, 400.0, 12.0, 3.0, 0.11111),
    _side("right_sized", "Medium", 4.0, 25.0, 0.0, 0.0, 0.02778),
]


def _changes(verdict):
    return {row[0]: row[3] for row in verdict.rows}


def test_comparison_shows_both_sides_and_a_verdict():
    side_by_side, verdict = report.comparison_tables(RESULTS, scenario=LOCAL)

    assert "Local spill" in side_by_side.title
    assert side_by_side.columns[0] == "warehouse"
    assert [row[0] for row in side_by_side.rows] == ["undersized", "right-sized"]
    assert side_by_side.rows[0][4] == "12.00"
    assert verdict.columns == ["metric", "undersized", "right-sized", "change"]
    assert _changes(verdict) == {
        "runtime (s)": "16.0x faster",
        "local spill (GB)": "eliminated",
        "remote spill (GB)": "eliminated",
        "est. credits": "75% cheaper",
    }


def test_comparison_handles_missing_stats():
    results = [
        _side("undersized", "X-Small", 1.0, 100.0, None, None, 0.02778),
        _side("right_sized", "Medium", 4.0, 50.0, None, None, 0.05556),
    ]
    changes = _changes(report.comparison_tables(results, scenario=LOCAL)[1])
    assert changes["local spill (GB)"] == "—"
    assert changes["est. credits"] == "100% pricier"


def test_capped_undersized_run_is_a_lower_bound():
    results = [
        _side("undersized", "X-Small", 1.0, 2700.0, 30.0, 0.0, 0.75, timed_out=True),
        _side("right_sized", "Medium", 4.0, 90.0, 0.0, 0.0, 0.1),
    ]
    side_by_side, verdict = report.comparison_tables(results, scenario=LOCAL)

    assert side_by_side.rows[0][3] == "2700.0+"
    changes = _changes(verdict)
    assert changes["runtime (s)"] == "at least 30.0x faster"
    assert changes["local spill (GB)"] == "eliminated"
    assert changes["est. credits"] == "—"


def test_capped_right_sized_run_claims_no_change():
    results = [
        _side("undersized", "X-Small", 1.0, 900.0, 30.0, 0.0, 0.25),
        _side("right_sized", "Medium", 4.0, 675.0, 5.0, 0.0, 0.75, timed_out=True),
    ]
    changes = _changes(report.comparison_tables(results, scenario=LOCAL)[1])
    assert set(changes.values()) == {"—"}


def test_one_side_has_no_verdict():
    assert len(report.comparison_tables(RESULTS[:1], scenario=LOCAL)) == 1


def test_read_report_runs_every_step_on_the_demo_warehouse(account):
    cursor, conn = account()

    tables = report.read_report(conn, objects=infra.DemoObjects.named("spillage_demo_wh"), hours=3)

    assert [t.step for t in tables] == [step for step, _, _ in queries.REPORT_STEPS]
    sql = cursor.executed
    use = sql.index("USE WAREHOUSE SPILLAGE_DEMO_WH")
    reports = [s for s in sql if "ACCOUNT_USAGE" in s]
    assert len(reports) == 3 and sql.index(reports[0]) > use
    assert all("warehouse_name = 'SPILLAGE_DEMO_WH'" in s for s in reports)  # upper-cased
    assert all("DATEADD('hour', -3," in s for s in reports)
    assert cursor.closed


def test_read_report_needs_setup(account):
    _cursor, conn = account(warehouse_comment=None)
    with pytest.raises(ValueError, match="spillage setup"):
        report.read_report(conn, objects=infra.DemoObjects.named())
