"""Unit tests for creating, checking, and dropping the benchmark's own objects."""

from __future__ import annotations

import pytest

from experiments.warehouse_sizing_benchmark.core import infra, queries

OBJECTS = infra.BenchmarkObjects.named()
IDLE = (
    "ALTER WAREHOUSE SIZING_BENCHMARK_WH SET WAREHOUSE_SIZE = XSMALL STATEMENT_TIMEOUT_IN_SECONDS = 1800 "
    "AUTO_SUSPEND = 60 AUTO_RESUME = TRUE"
)


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
    assert IDLE in sql
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


def test_setup_generates_the_table_on_a_medium_when_the_sample_data_is_unreadable(account):
    cursor, conn = account(sample_data=False)

    infra.setup(conn, OBJECTS)

    sql = cursor.executed
    ctas = next(i for i, s in enumerate(sql) if s.strip().startswith("CREATE TABLE"))
    assert sql[ctas].strip().startswith("CREATE TABLE SIZING_BENCHMARK_DB.PUBLIC.LINEITEM AS")
    assert (
        sql[ctas - 1]
        == "ALTER WAREHOUSE SIZING_BENCHMARK_WH SET WAREHOUSE_SIZE = MEDIUM STATEMENT_TIMEOUT_IN_SECONDS = 450"
    )
    assert sql[ctas + 1] == IDLE


def test_a_generation_timeout_is_a_clean_error_and_still_resets(account, snowflake_timeout):
    cursor, conn = account(sample_data=False, fail={"CREATE TABLE": [snowflake_timeout()]})

    with pytest.raises(ValueError, match="hit its 450s cap, so nothing was created"):
        infra.setup(conn, OBJECTS)
    assert cursor.executed[-2:] == [IDLE, "ALTER WAREHOUSE SIZING_BENCHMARK_WH SUSPEND"]


def test_setup_reuses_a_generated_table(account):
    cursor, conn = account(sample_data=False, generated_table=True)
    messages: list[str] = []
    infra.setup(conn, OBJECTS, echo=messages.append)
    assert not any(s.strip().startswith("CREATE TABLE") for s in cursor.executed)
    assert "Reusing table SIZING_BENCHMARK_DB.PUBLIC.LINEITEM." in messages


@pytest.mark.parametrize("kind", ["warehouse", "database"])
def test_setup_refuses_someone_elses_object(account, kind):
    cursor, conn = account(**{f"{kind}_comment": "production"})
    with pytest.raises(ValueError, match=f"{kind} SIZING_BENCHMARK_\\w+ already exists and this experiment didn't"):
        infra.setup(conn, OBJECTS)
    assert not any(s.startswith(("CREATE", "ALTER WAREHOUSE", "DROP")) for s in cursor.executed)


def test_a_taken_database_name_creates_nothing(account):
    cursor, conn = account(warehouse_comment=None, database_comment="production")
    with pytest.raises(ValueError, match="database SIZING_BENCHMARK_DB"):
        infra.setup(conn, OBJECTS)
    assert not any(s.startswith("CREATE") for s in cursor.executed)


def test_setup_refuses_a_generation_mismatch(account):
    _cursor, conn = account(generation="2")
    with pytest.raises(ValueError, match="already exists as Gen2"):
        infra.setup(conn, OBJECTS, generation="1")


def test_setup_warns_when_account_usage_is_unreadable(account):
    _cursor, conn = account(fail={"ACCOUNT_USAGE": [RuntimeError("not authorized")]})
    messages: list[str] = []
    infra.setup(conn, OBJECTS, echo=messages.append)
    assert any("can't read SNOWFLAKE.ACCOUNT_USAGE" in m for m in messages)


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


def test_require_warehouse_assumes_gen2_when_the_account_does_not_say(account):
    cursor, _conn = account(generation=None)
    messages: list[str] = []
    assert infra.require_warehouse(cursor, OBJECTS, echo=messages.append) == "2"
    assert "at the Gen2 rate to be safe" in messages[0]


def test_require_warehouse_needs_no_database_or_table(account):
    cursor, _conn = account(database_comment=None, sample_data=False)
    assert infra.require_warehouse(cursor, OBJECTS) == "1"


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


def test_cleanup_drops_both_objects(account):
    cursor, conn = account()
    infra.cleanup(conn, OBJECTS)
    assert "DROP WAREHOUSE IF EXISTS SIZING_BENCHMARK_WH" in cursor.executed
    assert "DROP DATABASE IF EXISTS SIZING_BENCHMARK_DB" in cursor.executed


def test_cleanup_is_idempotent(account):
    _cursor, conn = account(warehouse_comment=None, database_comment=None)
    messages: list[str] = []
    infra.cleanup(conn, OBJECTS, echo=messages.append)
    assert messages == ["No warehouse SIZING_BENCHMARK_WH to drop.", "No database SIZING_BENCHMARK_DB to drop."]


def test_cleanup_refuses_someone_elses_warehouse(account):
    cursor, conn = account(warehouse_comment="production")
    with pytest.raises(ValueError, match="this experiment didn't create it"):
        infra.cleanup(conn, OBJECTS)
    assert not any(s.startswith("DROP") for s in cursor.executed)


def test_reset_to_idle_never_raises(make_cursor):
    def connection_lost(sql):
        raise RuntimeError("connection lost")

    infra.reset_to_idle(make_cursor(route=connection_lost), "SIZING_BENCHMARK_WH")  # no exception
