"""Unit tests for the benchmark SQL and constants."""

from __future__ import annotations

import pytest

from experiments.warehouse_sizing_benchmark.core import queries


def _filled(step: int, **overrides) -> str:
    sql = dict((s, sql) for s, _, sql in queries.REPORT_STEPS)[step]
    args = {"warehouse": "SIZING_BENCHMARK_WH", "run_id": "R1", "hours": 6, "generation": "1"} | overrides
    return queries.report_sql(sql, **args)


def test_the_benchmark_query_is_the_articles():
    assert queries.BENCHMARK_QUERY.startswith("SELECT l_orderkey")
    assert "IDENTIFIER($lineitem_table)" in queries.BENCHMARK_QUERY
    assert "GROUP BY l_orderkey, l_suppkey" in queries.BENCHMARK_QUERY


def test_report_steps_cover_10_through_16():
    assert [step for step, _, _ in queries.REPORT_STEPS] == [10, 11, 12, 13, 14, 15, 16]


def test_every_step_fills_cleanly_and_filters_on_the_warehouse():
    for step, _, _ in queries.REPORT_STEPS:
        sql = _filled(step)
        assert "{" not in sql
        assert "warehouse_name = 'SIZING_BENCHMARK_WH'" in sql
        if step != 15:  # 15 is the warehouse's whole metering, not per query
            assert "query_tag LIKE 'wsbench:R1:%'" in sql
            assert "ILIKE 'SELECT l_orderkey%'" in sql


def test_the_rate_table_and_size_order_come_from_the_shared_sizes():
    assert "SELECT '2X-Large' sz, 32 cph, 6 ord" in _filled(12)
    assert "CASE warehouse_size WHEN 'X-Small' THEN 1" in _filled(13)
    assert "CASE q.warehouse_size WHEN 'X-Small' THEN 1" in _filled(16)


def test_estimated_credit_steps_price_the_generation():
    for step in (12, 14):
        assert "rate.cph * 1.35" in _filled(step, generation="2")


def test_cancelled_runs_are_counted_and_left_out_of_the_median():
    assert "COUNT_IF(NOT r.ok)" in _filled(12)
    assert "MEDIAN(IFF(r.ok, r.s, NULL))" in _filled(12)
    assert "agg.cancelled" in _filled(14)
    assert "execution_status" in _filled(11)


def test_step_15_starts_at_the_top_of_the_hour_and_has_no_row_of_nulls():
    sql = _filled(15)
    assert "DATE_TRUNC('hour'" in sql
    assert "HAVING COUNT(*) > 0" in sql


def test_step_16_bounds_query_history_too():
    sql = _filled(16)
    assert "q.start_time > DATEADD('hour', -6," in sql
    assert "a.start_time > DATEADD('hour', -6," in sql


@pytest.mark.parametrize(("overrides", "match"), [({"hours": 0}, "hours"), ({"run_id": "bad'id"}, "run id")])
def test_report_sql_rejects_unsafe_values(overrides, match):
    with pytest.raises(ValueError, match=match):
        _filled(10, **overrides)


def test_latest_run_ignores_tags_from_before_run_ids():
    sql = queries.LATEST_RUN_SQL.format(wh="SIZING_BENCHMARK_WH", hours=24)
    # Old tags look like wsbench:XXLARGE:1, which would sort after any timestamp.
    assert "REGEXP_LIKE(SPLIT_PART(query_tag, ':', 2), '[0-9]{8}-[0-9]{6}')" in sql


def test_live_stats_read_spill_from_operator_stats_and_elapsed_from_history():
    assert "GET_QUERY_OPERATOR_STATS(%s)" in queries.SPILL_SQL
    assert "spilling:bytes_spilled_local_storage" in queries.SPILL_SQL
    elapsed = queries.ELAPSED_SQL.format(database="SIZING_BENCHMARK_DB")
    assert "SIZING_BENCHMARK_DB.INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION" in elapsed
    assert "query_id = %s" in elapsed


def test_the_generated_table_matches_tpch_types():
    sql = queries.GENERATED_TABLE_SQL.format(table="DB.PUBLIC.LINEITEM", rows=queries.SOURCE_ROWS)
    assert sql.strip().startswith("CREATE TABLE DB.PUBLIC.LINEITEM AS")
    assert "GENERATOR(ROWCOUNT => 600000000)" in sql
    # TPC-H declares quantity, price, and discount as NUMBER(12, 2), which shapes the aggregates' width.
    assert sql.count("::NUMBER(12, 2)") == 3


@pytest.mark.parametrize("bad", ["bad'id", "x y", ""])
def test_validate_run_id(bad):
    assert queries.validate_run_id("20261001-143318") == "20261001-143318"
    with pytest.raises(ValueError, match="run id"):
        queries.validate_run_id(bad)
