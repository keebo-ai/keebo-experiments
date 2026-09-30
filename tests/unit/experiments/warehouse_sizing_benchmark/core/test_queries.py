"""Unit tests for the benchmark SQL/constants."""

from __future__ import annotations

import pytest

from common import warehouses
from experiments.warehouse_sizing_benchmark.core import queries


def test_sizes_and_query_are_consistent():
    assert queries.SIZES == warehouses.SIZES
    assert [row[0] for row in queries.SIZES] == queries.SIZE_KEYWORDS
    assert len(queries.SIZES) == 6
    assert "IDENTIFIER($lineitem_table)" in queries.BENCHMARK_QUERY
    assert queries.BENCHMARK_QUERY.startswith("SELECT l_orderkey")


def test_report_steps_cover_10_through_16():
    assert [step for step, _, _ in queries.REPORT_STEPS] == [10, 11, 12, 13, 14, 15, 16]


def test_report_steps_filter_on_the_warehouse_and_one_run():
    filled = [sql.format(hours=6, wh="SIZING_BENCHMARK_WH", tag="wsbench:R1:") for _, _, sql in queries.REPORT_STEPS]
    assert all("{" not in sql for sql in filled)
    assert all("warehouse_name = 'SIZING_BENCHMARK_WH'" in sql for sql in filled)
    for step, sql in zip([s for s, _, _ in queries.REPORT_STEPS], filled, strict=True):
        if step != 15:  # 15 is the warehouse's whole metering, not per query
            assert "query_tag LIKE 'wsbench:R1:%'" in sql
            assert "ILIKE 'SELECT l_orderkey%'" in sql
    assert "HAVING COUNT(*) > 0" in filled[5]  # step 15: no row of NULLs while metering lags


def test_query_timeouts_reserve_the_minimums_then_split_the_rest():
    # X-Small + Medium, 1 run each, 3 credits: 5/60 credits of minimums reserved,
    # then 1.4583 credits per query.
    timeouts = queries.query_timeouts(["XSMALL", "MEDIUM"], generation="1", runs=1, max_credits=3.0)
    assert timeouts == {"XSMALL": 5250, "MEDIUM": 1312}


def test_the_default_cap_fits_the_articles_full_sweep():
    timeouts = queries.query_timeouts(queries.SIZE_KEYWORDS, generation="1", runs=3, max_credits=3.0)
    assert timeouts["XSMALL"] == 390 and timeouts["XXLARGE"] == 12


def test_query_timeouts_price_the_generation():
    gen1 = queries.query_timeouts(["XSMALL"], generation="1", runs=1, max_credits=1.0)
    gen2 = queries.query_timeouts(["XSMALL"], generation="2", runs=1, max_credits=1.0)
    assert gen2["XSMALL"] < gen1["XSMALL"]


def test_query_timeouts_reject_a_cap_below_the_minimums():
    with pytest.raises(ValueError, match="doesn't cover Snowflake's 60-second minimum"):
        queries.query_timeouts(queries.SIZE_KEYWORDS, generation="1", runs=3, max_credits=1.0)


def test_query_timeouts_reject_a_cap_that_is_too_tight():
    with pytest.raises(ValueError, match="only"):
        queries.query_timeouts(queries.SIZE_KEYWORDS, generation="1", runs=3, max_credits=1.2)


def test_live_stats_read_spill_from_operator_stats_and_elapsed_from_history():
    assert "GET_QUERY_OPERATOR_STATS(%s)" in queries.SPILL_SQL
    assert "spilling:bytes_spilled_local_storage" in queries.SPILL_SQL
    elapsed = queries.ELAPSED_SQL.format(database="SIZING_BENCHMARK_DB")
    assert "SIZING_BENCHMARK_DB.INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION" in elapsed
    assert "query_id = %s" in elapsed


def test_generated_table_matches_the_benchmark_query():
    sql = queries.GENERATED_TABLE_SQL.format(table="DB.PUBLIC.LINEITEM", rows=queries.SOURCE_ROWS)
    assert sql.strip().startswith("CREATE TABLE DB.PUBLIC.LINEITEM AS")
    assert "GENERATOR(ROWCOUNT => 600000000)" in sql
    for column in ("l_orderkey", "l_suppkey", "l_quantity", "l_extendedprice", "l_discount"):
        assert f"AS {column}" in sql


def test_validate_identifier():
    assert queries.validate_identifier("SCHEMA.TABLE$1", "table") == "SCHEMA.TABLE$1"
    with pytest.raises(ValueError, match="warehouse"):
        queries.validate_identifier("bad; DROP", "warehouse")
