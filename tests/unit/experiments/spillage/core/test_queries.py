"""Unit tests for the spillage workload, scenarios, and SQL constants."""

from __future__ import annotations

import pytest

from experiments.spillage.core import queries


def test_plain_workload_is_a_global_sort_with_tiny_output():
    sql = queries.build_workload(1)
    assert sql.startswith("SELECT COUNT(*) AS sorted_rows")
    assert "ROW_NUMBER() OVER" in sql
    assert "PARTITION BY" not in sql  # one global window is what forces the spill
    assert "IDENTIFIER($spill_table)" in sql
    assert "GENERATOR" not in sql


def test_fanout_amplifies_the_sort():
    sql = queries.build_workload(4)
    assert "GENERATOR(ROWCOUNT => 4)" in sql
    assert "SEQ4() AS seq" in sql
    assert "fan.seq" in sql


@pytest.mark.parametrize("bad", [0, -3])
def test_fanout_must_be_positive(bad):
    with pytest.raises(ValueError, match="fanout"):
        queries.build_workload(bad)


def test_scenarios_pair_an_undersized_and_a_right_sized_warehouse():
    assert set(queries.SCENARIOS) == {"local", "remote"}
    for scenario in queries.SCENARIOS.values():
        assert queries.CREDITS_PER_HOUR[scenario.undersized] < queries.CREDITS_PER_HOUR[scenario.right_sized]
    assert queries.SCENARIOS["remote"].fanout > queries.SCENARIOS["local"].fanout


def test_resolve_scenario_applies_overrides():
    scenario = queries.resolve_scenario("LOCAL", fanout=2, undersized="small", right_sized="xlarge")
    assert scenario.name == "local"
    assert (scenario.fanout, scenario.undersized, scenario.right_sized) == (2, "SMALL", "XLARGE")
    assert scenario.table == queries.DEFAULT_TABLE


def test_resolve_scenario_rejects_unknown_names():
    with pytest.raises(ValueError, match="unknown scenario"):
        queries.resolve_scenario("cloud")


def test_statement_timeout_splits_the_budget_across_sizes():
    # 1.5 credits -> 0.75 per size: 45 min at 1 credit/hr, ~11 min at 4.
    assert queries.statement_timeout_s("XSMALL", max_credits=1.5) == 2700
    assert queries.statement_timeout_s("MEDIUM", max_credits=1.5) == 675
    assert queries.statement_timeout_s("XSMALL", max_credits=1.5, runs=3) == 900


def test_statement_timeout_rejects_budgets_under_the_billing_minimum():
    with pytest.raises(ValueError, match="under 60 seconds"):
        queries.statement_timeout_s("MEDIUM", max_credits=0.05)


def test_live_stats_binds_the_query_id():
    assert "QUERY_HISTORY_BY_SESSION" in queries.LIVE_STATS_SQL
    assert "query_id = %s" in queries.LIVE_STATS_SQL


def test_report_steps_format_cleanly():
    for _step, _title, sql in queries.REPORT_STEPS:
        formatted = sql.format(hours=6, wh="SPILLAGE_DEMO_WH")
        assert "{" not in formatted
