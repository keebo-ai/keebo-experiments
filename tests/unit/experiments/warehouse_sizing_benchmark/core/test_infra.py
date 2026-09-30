"""Unit tests for creating, checking, and dropping the benchmark's own objects."""

from __future__ import annotations

import pytest

from experiments.warehouse_sizing_benchmark.core import infra, queries

OBJECTS = infra.BenchmarkObjects.named()


def test_named_validates_and_upper_cases():
    objects = infra.BenchmarkObjects.named("my_wh", "my_db")
    assert (objects.warehouse, objects.database) == ("MY_WH", "MY_DB")
    assert objects.generated_table == "MY_DB.PUBLIC.LINEITEM"
    with pytest.raises(ValueError, match="warehouse"):
        infra.BenchmarkObjects.named("bad; DROP", "DB")


def test_setup_creates_the_objects_and_uses_the_sample_data(account):
    cursor, conn = account(warehouse_comment=None, database_comment=None)
    messages: list[str] = []

    infra.setup(conn, OBJECTS, echo=messages.append)

    sql = cursor.executed
    create_wh = next(s for s in sql if s.startswith("CREATE WAREHOUSE"))
    assert "CREATE WAREHOUSE SIZING_BENCHMARK_WH " in create_wh
    assert "GENERATION = '1'" in create_wh
    assert f"COMMENT = '{queries.OWNER_COMMENT}'" in create_wh
    assert any(s.startswith("CREATE TRANSIENT DATABASE SIZING_BENCHMARK_DB") for s in sql)
    assert not any(s.strip().startswith("CREATE TABLE") for s in sql)
    assert any("Using Snowflake's sample data" in m for m in messages)
    assert sql[-1] == "ALTER WAREHOUSE SIZING_BENCHMARK_WH SUSPEND"
    assert cursor.closed


def test_setup_is_idempotent(account):
    cursor, conn = account()
    messages: list[str] = []

    infra.setup(conn, OBJECTS, echo=messages.append)

    assert not any(s.strip().startswith("CREATE") for s in cursor.executed)
    assert "Reusing warehouse SIZING_BENCHMARK_WH." in messages
    assert "Reusing database SIZING_BENCHMARK_DB." in messages


def test_setup_generates_the_table_when_the_sample_data_is_unreadable(account):
    cursor, conn = account(sample_data=False)

    infra.setup(conn, OBJECTS)

    ctas = next(s for s in cursor.executed if s.strip().startswith("CREATE TABLE"))
    assert ctas.strip().startswith("CREATE TABLE SIZING_BENCHMARK_DB.PUBLIC.LINEITEM AS")
    resize = "ALTER WAREHOUSE SIZING_BENCHMARK_WH SET WAREHOUSE_SIZE = XSMALL STATEMENT_TIMEOUT_IN_SECONDS = 1800"
    assert cursor.executed.index(resize) < cursor.executed.index(ctas)


def test_setup_reuses_a_generated_table(account):
    cursor, conn = account(sample_data=False, generated_table=True)
    messages: list[str] = []
    infra.setup(conn, OBJECTS, echo=messages.append)
    assert not any(s.strip().startswith("CREATE TABLE") for s in cursor.executed)
    assert "Reusing table SIZING_BENCHMARK_DB.PUBLIC.LINEITEM." in messages


@pytest.mark.parametrize("kind", ["warehouse", "database"])
def test_setup_refuses_someone_elses_object(account, kind):
    cursor, conn = account(**{f"{kind}_comment": "production"})
    with pytest.raises(ValueError, match=f"wasn't created by this experiment.*--{kind}"):
        infra.setup(conn, OBJECTS)
    assert not any(s.startswith(("CREATE", "ALTER WAREHOUSE", "DROP")) for s in cursor.executed)


def test_setup_refuses_a_generation_mismatch(account):
    _cursor, conn = account(generation="2")
    with pytest.raises(ValueError, match="already exists as Gen2"):
        infra.setup(conn, OBJECTS, generation="1")


def test_require_picks_the_sample_then_the_generated_table(account):
    cursor, _conn = account()
    assert infra.require(cursor, OBJECTS) == infra.BenchmarkState("1", queries.DEFAULT_TABLE)
    cursor, _conn = account(sample_data=False, generated_table=True)
    assert infra.require(cursor, OBJECTS).table == "SIZING_BENCHMARK_DB.PUBLIC.LINEITEM"


def test_require_checks_an_explicit_table(account):
    cursor, _conn = account()
    table = "SNOWFLAKE_SAMPLE_DATA.TPCH_SF1000.LINEITEM"
    assert infra.require(cursor, OBJECTS, table=table).table == table
    with pytest.raises(ValueError, match="fully qualified"):
        infra.require(cursor, OBJECTS, table="LINEITEM")


def test_require_assumes_gen2_when_the_account_does_not_say(account):
    cursor, _conn = account(generation=None)
    messages: list[str] = []
    assert infra.require(cursor, OBJECTS, echo=messages.append).generation == "2"
    assert "assume Gen2" in messages[0]


@pytest.mark.parametrize(
    ("settings", "match"),
    [
        ({"warehouse_comment": None}, "warehouse-sizing setup"),
        ({"database_comment": None}, "warehouse-sizing setup"),
        ({"sample_data": False}, "no table to read"),
    ],
)
def test_require_points_at_setup(account, settings, match):
    cursor, _conn = account(**settings)
    with pytest.raises(ValueError, match=match):
        infra.require(cursor, OBJECTS)


def test_cleanup_is_idempotent_and_guarded(account):
    cursor, conn = account()
    messages: list[str] = []
    infra.cleanup(conn, OBJECTS, echo=messages.append)
    assert "DROP WAREHOUSE IF EXISTS SIZING_BENCHMARK_WH" in cursor.executed
    assert "DROP DATABASE IF EXISTS SIZING_BENCHMARK_DB" in cursor.executed

    _cursor, conn = account(warehouse_comment=None, database_comment=None)
    messages = []
    infra.cleanup(conn, OBJECTS, echo=messages.append)
    assert messages == ["No warehouse SIZING_BENCHMARK_WH to drop.", "No database SIZING_BENCHMARK_DB to drop."]

    cursor, conn = account(warehouse_comment="production")
    with pytest.raises(ValueError, match="--warehouse"):
        infra.cleanup(conn, OBJECTS)
    assert not any(s.startswith("DROP") for s in cursor.executed)
