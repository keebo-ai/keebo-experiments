"""Unit tests for the spillage workload, scenarios, cost cap, and SQL."""

from __future__ import annotations

import pytest

from experiments.spillage.core import queries

TABLE = "SPILLAGE_DEMO_DB.PUBLIC.LINEITEM"


def test_workload_is_one_global_sort_that_cannot_be_skipped():
    sql = queries.build_workload(TABLE, 1)
    assert sql.startswith(queries.WORKLOAD_PREFIX)
    assert "ROW_NUMBER() OVER (" in sql
    assert "PARTITION BY" not in sql  # one global window is what forces the spill
    assert f"FROM {TABLE}" in sql
    assert "GENERATOR" not in sql


def test_fanout_multiplies_the_rows_sorted():
    sql = queries.build_workload(TABLE, 4)
    assert "GENERATOR(ROWCOUNT => 4)" in sql
    assert "fan.seq" in sql


@pytest.mark.parametrize("bad", [0, -3])
def test_fanout_must_be_positive(bad):
    with pytest.raises(ValueError, match="fanout"):
        queries.build_workload(TABLE, bad)


def test_fallback_table_is_generated_with_the_sample_tables_shape():
    sql = queries.GENERATED_TABLE_SQL.format(table=TABLE, rows=queries.SOURCE_ROWS)
    assert sql.strip().startswith(f"CREATE TABLE {TABLE} AS")
    assert "GENERATOR(ROWCOUNT => 60000000)" in sql
    assert "SNOWFLAKE_SAMPLE_DATA" not in sql
    for column in ("l_extendedprice", "l_discount", "l_shipdate", "l_orderkey", "l_partkey", "l_suppkey"):
        assert f"AS {column}" in sql  # every column the workload sorts on


def test_scenarios_compare_xsmall_with_medium():
    assert set(queries.SCENARIOS) == {"local", "remote"}
    for scenario in queries.SCENARIOS.values():
        assert (scenario.undersized, scenario.right_sized) == ("XSMALL", "MEDIUM")
    assert queries.SCENARIOS["remote"].fanout > queries.SCENARIOS["local"].fanout


def test_resolve_scenario_applies_overrides():
    scenario = queries.resolve_scenario("LOCAL", fanout=2, undersized="small", right_sized="large")
    assert (scenario.name, scenario.fanout, scenario.undersized, scenario.right_sized) == ("local", 2, "SMALL", "LARGE")


def test_resolve_scenario_requires_undersized_to_be_smaller():
    with pytest.raises(ValueError, match="must be smaller"):
        queries.resolve_scenario("local", undersized="large", right_sized="medium")


def test_resolve_scenario_rejects_unknown_names():
    with pytest.raises(ValueError, match="unknown scenario"):
        queries.resolve_scenario("cloud")


def test_statement_timeout_splits_the_budget_and_prices_the_generation():
    # 1.5 credits -> 0.75 per size: 45 min on a Gen1 X-Small, ~11 min on a Gen1 Medium.
    assert queries.statement_timeout_s("XSMALL", generation="1", max_credits=1.5) == 2700
    assert queries.statement_timeout_s("MEDIUM", generation="1", max_credits=1.5) == 675
    # Gen2 bills 1.35x, so the same budget buys less time (1999.99s, rounded down to stay inside the cap).
    assert queries.statement_timeout_s("XSMALL", generation="2", max_credits=1.5) == 1999


def test_statement_timeout_rejects_budgets_under_the_billing_minimum():
    with pytest.raises(ValueError, match="under 60 seconds"):
        queries.statement_timeout_s("MEDIUM", generation="1", max_credits=0.05)


def test_live_stats_reads_spill_from_operator_stats_and_elapsed_from_history():
    sql = queries.LIVE_STATS_SQL.format(database="SPILLAGE_DEMO_DB")
    # INFORMATION_SCHEMA's query history has no spill columns; operator stats do.
    assert "GET_QUERY_OPERATOR_STATS(%s)" in sql
    assert "spilling:bytes_spilled_local_storage" in sql
    assert "spilling:bytes_spilled_remote_storage" in sql
    assert "bytes_spilled_to_" not in sql
    assert "SPILLAGE_DEMO_DB.INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION" in sql
    assert sql.count("%s") == 2  # the query id, bound twice


def test_report_steps_filter_on_the_run_and_group_per_run():
    filled = [
        sql.format(wh="SPILLAGE_DEMO_WH", hours=6, tag=queries.QUERY_TAG_PREFIX, workload=queries.WORKLOAD_PREFIX)
        for _, _, sql in queries.REPORT_STEPS
    ]
    assert all("{" not in sql for sql in filled)
    assert all("warehouse_name = 'SPILLAGE_DEMO_WH'" in sql for sql in filled)
    for sql in filled[:2]:  # the per-query steps
        assert "query_tag LIKE 'spill:%'" in sql
        assert f"query_text ILIKE '{queries.WORKLOAD_PREFIX}%'" in sql
    assert "GROUP BY 1, 2, 3, 4" in filled[1]  # run, scenario, side, size
    assert "HAVING COUNT(*) > 0" in filled[2]  # empty, not a row of NULLs, while metering lags
