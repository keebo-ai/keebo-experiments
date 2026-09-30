"""Unit tests for the spillage report layer."""

from __future__ import annotations

from experiments.spillage.core import queries, report
from experiments.spillage.core.run import ArmResult


def _arm(arm, keyword, label, cph, runtime, local, remote, credits, timed_out=False):
    return ArmResult(
        arm=arm,
        size_keyword=keyword,
        size_label=label,
        credits_per_hour=cph,
        runtime_s=runtime,
        elapsed_s=runtime,
        gb_spill_local=local,
        gb_spill_remote=remote,
        partitions_scanned=10,
        partitions_total=10,
        est_credits=credits,
        query_id=f"qid-{arm}",
        timed_out=timed_out,
    )


RESULTS = [
    _arm("undersized", "XSMALL", "X-Small", 1, 400.0, 12.0, 3.0, 0.11111),
    _arm("right_sized", "LARGE", "Large", 8, 25.0, 0.0, 0.0, 0.05556),
]


def test_comparison_tables_shows_both_arms_and_a_verdict():
    tables = report.comparison_tables(RESULTS, scenario=queries.SCENARIOS["local"])

    assert [t.step for t in tables] == [1, 2]
    ab, verdict = tables
    assert "Local spill" in ab.title
    assert [row[0] for row in ab.rows] == ["undersized", "right-sized"]
    assert ab.rows[0][4] == "12.00"

    changes = {row[0]: row[3] for row in verdict.rows}
    assert changes["runtime (s)"] == "16.0x faster"
    assert changes["local spill (GB)"] == "eliminated"
    assert changes["remote spill (GB)"] == "eliminated"
    assert changes["est. credits"] == "50% cheaper"


def test_comparison_tables_handles_missing_stats():
    results = [
        _arm("undersized", "XSMALL", "X-Small", 1, 100.0, None, None, 0.02778),
        _arm("right_sized", "LARGE", "Large", 8, 50.0, None, None, 0.11111),
    ]
    _ab, verdict = report.comparison_tables(results, scenario=queries.SCENARIOS["local"])

    changes = {row[0]: row[3] for row in verdict.rows}
    assert changes["local spill (GB)"] == "—"
    assert changes["est. credits"] == "300% pricier"


def test_capped_run_is_shown_as_a_lower_bound():
    results = [
        _arm("undersized", "XSMALL", "X-Small", 1, 2700.0, 30.0, 0.0, 0.75, timed_out=True),
        _arm("right_sized", "MEDIUM", "Medium", 4, 90.0, 0.0, 0.0, 0.1),
    ]
    ab, verdict = report.comparison_tables(results, scenario=queries.SCENARIOS["local"])

    assert ab.rows[0][3] == "2700.0+"
    changes = {row[0]: row[3] for row in verdict.rows}
    assert changes["runtime (s)"] == "at least 30.0x faster"
    assert changes["local spill (GB)"] == "eliminated"
    assert changes["est. credits"] == "—"


def test_comparison_tables_without_both_arms_has_no_verdict():
    tables = report.comparison_tables(RESULTS[:1], scenario=queries.SCENARIOS["local"])
    assert len(tables) == 1


def test_read_report_runs_every_step(make_cursor, make_connection):
    cursor = make_cursor(description=[("query_tag",)], fetch=[("spill:local:undersized:1",)])

    tables = report.read_report(make_connection(cursor), hours=3)

    assert [t.step for t in tables] == [step for step, _, _ in queries.REPORT_STEPS]
    assert all("'spill:%'" in s or "SPILLAGE_DEMO_WH" in s for s in cursor.executed)
    assert any("DATEADD('hour', -3," in s for s in cursor.executed)
    assert tables[0].columns == ["query_tag"]
    assert cursor.closed
