"""The adaptive-compute experiment's SQL and constants."""

from __future__ import annotations

import pytest

from experiments.adaptive_compute.core import queries

SCHEMA = "SNOWFLAKE_SAMPLE_DATA.TPCH_SF10"
TABLE = "DB.PUBLIC.T_X"
BASE = "DB.PUBLIC.LINEITEM_SF10"


def test_all_22_tpch_queries_fill_in_cleanly():
    assert list(queries.TPCH) == [f"q{n:02d}" for n in range(1, 23)]
    for name, sql in queries.TPCH.items():
        text = sql.format(s=SCHEMA)
        assert "{" not in text and "}" not in text, name
        assert SCHEMA.lower() in text.lower(), name


@pytest.mark.parametrize(
    ("workload", "names"),
    [
        (queries.SINGLE, ["q01"]),
        (queries.SEQUENTIAL, list(queries.TPCH)),
        (queries.CONCURRENT, list(queries.TPCH)),
        (queries.DML, ["delete_month", "insert_month"]),
        (queries.CTAS, ["ctas_narrow", "ctas_wide"]),
        (queries.MONSTER, ["monster"]),
        (queries.MONSTER_SPLIT, [f"b{i:02d}" for i in range(1, 17)] + ["combine"]),
    ],
)
def test_each_workload_runs_its_statements(workload, names):
    statements = queries.workload_statements(workload, schema=SCHEMA, table=TABLE, base=BASE)
    assert [name for name, _ in statements] == names


def test_dml_deletes_one_month_from_the_clone_and_reinserts_it_from_the_sorted_copy():
    (_, delete), (_, insert) = queries.workload_statements(queries.DML, schema=SCHEMA, table=TABLE, base=BASE)
    assert delete.startswith(f"DELETE FROM {TABLE} WHERE l_shipdate >= '1995-06-01'::DATE")
    assert insert.startswith(f"INSERT INTO {TABLE} SELECT * FROM {BASE} WHERE")
    assert queries.prepare_statements(queries.DML, table=TABLE, base=BASE) == [f"CREATE TABLE {TABLE} CLONE {BASE}"]
    assert queries.teardown_statements(queries.DML, table=TABLE) == [f"DROP TABLE IF EXISTS {TABLE}"]


def test_ctas_writes_two_scratch_tables_and_drops_both():
    statements = queries.workload_statements(queries.CTAS, schema=SCHEMA, table=TABLE, base=BASE)
    assert [sql.split(" AS ")[0] for _, sql in statements] == [
        f"CREATE TABLE {TABLE}_NARROW",
        f"CREATE TABLE {TABLE}_WIDE",
    ]
    assert queries.teardown_statements(queries.CTAS, table=TABLE) == [
        f"DROP TABLE IF EXISTS {TABLE}_NARROW",
        f"DROP TABLE IF EXISTS {TABLE}_WIDE",
    ]
    assert queries.prepare_statements(queries.CTAS, table=TABLE, base=BASE) == []


def test_the_monster_workloads_read_the_chosen_scale_and_fanout():
    sf100 = "SNOWFLAKE_SAMPLE_DATA.TPCH_SF100"
    ((_, sql),) = queries.workload_statements(queries.MONSTER, schema=sf100, table=TABLE, base=BASE, fanout=4)
    assert f"FROM {sf100}.lineitem" in sql
    assert sql.count(" AS (\n") == 3 * 4 - 1


def test_monster_split_runs_in_two_stages_and_drops_every_branch_table():
    stages = queries.workload_stages(queries.MONSTER_SPLIT, schema=SCHEMA, table=TABLE, base=BASE, fanout=3)
    assert [[name for name, _ in stage] for stage in stages] == [["b01", "b02", "b03"], ["combine"]]
    assert all(sql.startswith(f"CREATE TEMPORARY TABLE {TABLE}_B0") for _, sql in stages[0])
    assert queries.teardown_statements(queries.MONSTER_SPLIT, table=TABLE, fanout=3) == [
        f"DROP TABLE IF EXISTS {TABLE}_B01",
        f"DROP TABLE IF EXISTS {TABLE}_B02",
        f"DROP TABLE IF EXISTS {TABLE}_B03",
    ]
    assert queries.prepare_statements(queries.MONSTER_SPLIT, table=TABLE, base=BASE) == []
    assert queries.teardown_statements(queries.MONSTER, table=TABLE) == []


def test_the_concurrent_workload_is_one_stage_and_sequential_ones_have_none():
    (stage,) = queries.workload_stages(queries.CONCURRENT, schema=SCHEMA, table=TABLE, base=BASE)
    assert [name for name, _ in stage] == list(queries.TPCH)
    with pytest.raises(ValueError, match="one statement at a time"):
        queries.workload_stages(queries.MONSTER, schema=SCHEMA, table=TABLE, base=BASE)


def _ddl(config, *, concurrent=False, max_clusters=2):
    return queries.create_cell_warehouse_sql(
        "MY_WH",
        config=config,
        size="SMALL",
        concurrent=concurrent,
        max_clusters=max_clusters,
        throughput_multiplier=2,
        timeout_s=120,
    )


def test_adaptive_ddl_sets_the_performance_level_not_a_size():
    sql = _ddl(queries.ADAPTIVE, concurrent=True)
    assert "WAREHOUSE_TYPE = 'ADAPTIVE'" in sql
    assert "MAX_QUERY_PERFORMANCE_LEVEL = SMALL QUERY_THROUGHPUT_MULTIPLIER = 2" in sql
    for standard_only in ("WAREHOUSE_SIZE", "GENERATION", "CLUSTER_COUNT", "AUTO_SUSPEND"):
        assert standard_only not in sql
    assert "STATEMENT_TIMEOUT_IN_SECONDS = 120 STATEMENT_QUEUED_TIMEOUT_IN_SECONDS = 120" in sql
    assert f"COMMENT = '{queries.OWNER_COMMENT}'" in sql


@pytest.mark.parametrize(("config", "generation"), [(queries.GEN1, "1"), (queries.GEN2, "2")])
def test_standard_ddl_always_pins_the_generation(config, generation):
    # Snowflake's default generation is now '2', so Gen1 has to be asked for.
    sql = _ddl(config)
    assert f"WAREHOUSE_SIZE = SMALL GENERATION = '{generation}'" in sql
    assert "INITIALLY_SUSPENDED = TRUE" in sql


def test_only_the_concurrent_workload_gets_more_than_one_cluster():
    assert "MAX_CLUSTER_COUNT = 2" in _ddl(queries.GEN1, concurrent=True)
    assert "CLUSTER_COUNT" not in _ddl(queries.GEN1, concurrent=False)
    assert "CLUSTER_COUNT" not in _ddl(queries.GEN1, concurrent=True, max_clusters=1)


def test_ddl_refuses_an_unsafe_name():
    with pytest.raises(ValueError, match="single unquoted name"):
        queries.create_cell_warehouse_sql(
            "x; DROP", config=queries.GEN1, size="SMALL", concurrent=False, max_clusters=1,
            throughput_multiplier=2, timeout_s=60,
        )  # fmt: skip


def test_scale_and_run_id_are_validated():
    assert queries.sample_schema(100) == "SNOWFLAKE_SAMPLE_DATA.TPCH_SF100"
    with pytest.raises(ValueError, match="scale"):
        queries.sample_schema(3)
    assert queries.validate_run_id("20261014_143318") == "20261014_143318"
    with pytest.raises(ValueError, match="run id"):
        queries.validate_run_id("20261014'; --")


def test_report_sql_fills_the_run_tag_and_cell_prefix():
    sql = queries.report_sql(queries.CELLS_SQL, home="my_wh", run_id="20261014_143318", hours=6)
    assert "query_tag LIKE 'adcomp:20261014_143318:%'" in sql
    assert "DATEADD('hour', -6," in sql
    assert "query_type IN ('SELECT', 'INSERT', 'DELETE', 'CREATE_TABLE_AS_SELECT')" in sql
    for column in (
        "compilation_time / 1000.0",
        "execution_time / 1000.0",
        "queued_provisioning_time / 1000.0",
        "queued_overload_time / 1000.0",
        "SUM(q.spilled_local) / POWER(1024, 3)",
        "SUM(q.spilled_remote) / POWER(1024, 3)",
        "SUM(q.cloud_services)",
    ):
        assert column in sql, column
    metering = queries.report_sql(queries.METERING_SQL, home="my_wh", run_id="20261014_143318", hours=6)
    assert "STARTSWITH(warehouse_name, 'MY_WH_20261014_143318_')" in metering
    with pytest.raises(ValueError, match="hours"):
        queries.report_sql(queries.CELLS_SQL, home="WH", run_id="20261014_143318", hours=0)
