"""Unit tests for creating, checking, and dropping the demo's own objects."""

from __future__ import annotations

import pytest

from experiments.spillage.core import infra, queries

OBJECTS = infra.DemoObjects.named()


def test_named_validates_and_upper_cases():
    objects = infra.DemoObjects.named("my_wh", "my_db")
    assert (objects.warehouse, objects.database) == ("MY_WH", "MY_DB")
    assert objects.generated_table == "MY_DB.PUBLIC.LINEITEM"


def test_named_rejects_unsafe_names():
    with pytest.raises(ValueError, match="warehouse"):
        infra.DemoObjects.named("bad; DROP", "DB")


def test_setup_creates_the_warehouse_and_database_and_uses_the_sample_data(account):
    cursor, conn = account(warehouse_comment=None, database_comment=None)
    messages: list[str] = []

    infra.setup(conn, OBJECTS, echo=messages.append)

    sql = cursor.executed
    create_wh = next(s for s in sql if s.startswith("CREATE WAREHOUSE"))
    assert "CREATE WAREHOUSE SPILLAGE_DEMO_WH " in create_wh
    assert "WAREHOUSE_SIZE = XSMALL" in create_wh
    assert "GENERATION = '1'" in create_wh
    assert "AUTO_SUSPEND = 60" in create_wh
    assert f"COMMENT = '{queries.OWNER_COMMENT}'" in create_wh
    create_db = next(s for s in sql if s.startswith("CREATE TRANSIENT DATABASE"))
    assert "DATA_RETENTION_TIME_IN_DAYS = 0" in create_db
    assert "SHOW TABLES LIKE 'LINEITEM' IN SCHEMA SNOWFLAKE_SAMPLE_DATA.TPCH_SF10" in sql
    assert not any(s.strip().startswith("CREATE TABLE") for s in sql)  # nothing to generate
    assert any("Using Snowflake's sample data, SNOWFLAKE_SAMPLE_DATA.TPCH_SF10.LINEITEM" in m for m in messages)
    assert sql[-1] == "ALTER WAREHOUSE SPILLAGE_DEMO_WH SUSPEND"
    assert "Using role SYSADMIN" in messages[0]
    assert cursor.closed


def test_setup_generates_the_table_when_the_sample_data_is_unreadable(account):
    cursor, conn = account(warehouse_comment=None, database_comment=None, sample_data=False)
    messages: list[str] = []

    infra.setup(conn, OBJECTS, echo=messages.append)

    sql = cursor.executed
    ctas = next(s for s in sql if s.strip().startswith("CREATE TABLE"))
    assert ctas.strip().startswith("CREATE TABLE SPILLAGE_DEMO_DB.PUBLIC.LINEITEM AS")
    # Generated on an X-Small with its own timeout, then suspended.
    resize = "ALTER WAREHOUSE SPILLAGE_DEMO_WH SET WAREHOUSE_SIZE = XSMALL STATEMENT_TIMEOUT_IN_SECONDS = 900"
    assert sql.index(resize) < sql.index(ctas)
    assert sql[-1] == "ALTER WAREHOUSE SPILLAGE_DEMO_WH SUSPEND"
    assert any("can't read SNOWFLAKE_SAMPLE_DATA.TPCH_SF10.LINEITEM, so generating" in m for m in messages)


def test_setup_reuses_its_own_objects(account):
    cursor, conn = account(sample_data=False, generated_table=True)
    messages: list[str] = []

    infra.setup(conn, OBJECTS, echo=messages.append)

    assert not any(s.strip().startswith("CREATE") for s in cursor.executed)
    assert "Reusing warehouse SPILLAGE_DEMO_WH." in messages
    assert "Reusing database SPILLAGE_DEMO_DB." in messages
    assert "Reusing table SPILLAGE_DEMO_DB.PUBLIC.LINEITEM." in messages


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


def test_setup_warns_when_account_usage_is_unreadable(account):
    cursor, conn = account()
    real_execute = cursor.execute

    def execute(sql, *args):
        real_execute(sql, *args)
        if sql == queries.ACCOUNT_USAGE_PROBE:
            raise RuntimeError("not authorized")
        return cursor

    cursor.execute = execute
    messages: list[str] = []

    infra.setup(conn, OBJECTS, echo=messages.append)

    assert any("can't read SNOWFLAKE.ACCOUNT_USAGE" in m for m in messages)


def test_require_returns_the_generation_and_the_sample_table(account):
    cursor, _conn = account(generation="2")
    assert infra.require(cursor, OBJECTS) == infra.DemoState("2", queries.SAMPLE_TABLE)


def test_require_falls_back_to_the_generated_table(account):
    cursor, _conn = account(sample_data=False, generated_table=True)
    assert infra.require(cursor, OBJECTS).source_table == "SPILLAGE_DEMO_DB.PUBLIC.LINEITEM"


def test_require_assumes_gen2_when_the_account_does_not_say(account):
    cursor, _conn = account(generation=None)
    messages: list[str] = []
    assert infra.require(cursor, OBJECTS, echo=messages.append).generation == "2"  # the pricier rate keeps the cap safe
    assert "assume Gen2" in messages[0]


def test_require_points_at_setup_when_there_is_no_table_to_read(account):
    cursor, _conn = account(sample_data=False, generated_table=False)
    with pytest.raises(ValueError, match="no table to read.*setup didn't finish"):
        infra.require(cursor, OBJECTS)


@pytest.mark.parametrize("missing", ["warehouse", "database"])
def test_require_points_at_setup_when_objects_are_missing(account, missing):
    cursor, _conn = account(**{f"{missing}_comment": None})
    with pytest.raises(ValueError, match="spillage setup"):
        infra.require(cursor, OBJECTS)


def test_cleanup_drops_only_its_own_objects(account):
    cursor, conn = account()
    messages: list[str] = []

    infra.cleanup(conn, OBJECTS, echo=messages.append)

    assert "DROP WAREHOUSE IF EXISTS SPILLAGE_DEMO_WH" in cursor.executed
    assert "DROP DATABASE IF EXISTS SPILLAGE_DEMO_DB" in cursor.executed
    assert messages == ["Dropped warehouse SPILLAGE_DEMO_WH.", "Dropped database SPILLAGE_DEMO_DB."]


def test_cleanup_refuses_someone_elses_warehouse(account):
    cursor, conn = account(warehouse_comment="production")
    with pytest.raises(ValueError, match="--warehouse"):
        infra.cleanup(conn, OBJECTS)
    assert not any(s.startswith("DROP") for s in cursor.executed)


def test_cleanup_reports_nothing_to_drop(account):
    _cursor, conn = account(warehouse_comment=None, database_comment=None)
    messages: list[str] = []
    infra.cleanup(conn, OBJECTS, echo=messages.append)
    assert messages == ["No warehouse SPILLAGE_DEMO_WH to drop.", "No database SPILLAGE_DEMO_DB to drop."]


def test_suspend_quietly_swallows_errors(make_cursor):
    cursor = make_cursor()

    def execute(sql, *args):
        raise RuntimeError("Invalid state. Warehouse cannot be suspended.")

    cursor.execute = execute
    messages: list[str] = []

    infra.suspend_quietly(cursor, "WH", messages.append)

    assert "couldn't suspend WH" in messages[0]
