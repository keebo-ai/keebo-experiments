"""Unit tests for creating, checking, and dropping the demo's own objects."""

from __future__ import annotations

import pytest

from experiments.spillage.core import infra, queries

OBJECTS = infra.DemoObjects.named()


def test_named_validates_and_upper_cases():
    objects = infra.DemoObjects.named("my_wh", "my_db")
    assert (objects.warehouse, objects.database) == ("MY_WH", "MY_DB")
    assert objects.table == "MY_DB.PUBLIC.LINEITEM"


def test_named_rejects_unsafe_names():
    with pytest.raises(ValueError, match="warehouse"):
        infra.DemoObjects.named("bad; DROP", "DB")


def test_setup_creates_everything_on_a_fresh_account(account):
    cursor, conn = account(warehouse_comment=None, database_comment=None, table_exists=False)
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
    assert "SHOW TABLES LIKE 'LINEITEM' IN SCHEMA SPILLAGE_DEMO_DB.PUBLIC" in sql
    assert any(s.strip().startswith("CREATE TABLE SPILLAGE_DEMO_DB.PUBLIC.LINEITEM AS") for s in sql)
    # The table is generated on an X-Small with a timeout, then the warehouse is suspended.
    assert "ALTER WAREHOUSE SPILLAGE_DEMO_WH SET WAREHOUSE_SIZE = XSMALL STATEMENT_TIMEOUT_IN_SECONDS = 900" in sql
    assert sql[-1] == "ALTER WAREHOUSE SPILLAGE_DEMO_WH SUSPEND"
    assert "Using role SYSADMIN" in messages[0]
    assert cursor.closed


def test_setup_reuses_its_own_objects(account):
    cursor, conn = account()
    messages: list[str] = []

    infra.setup(conn, OBJECTS, echo=messages.append)

    assert not any(s.strip().startswith("CREATE") for s in cursor.executed)
    assert "Reusing warehouse SPILLAGE_DEMO_WH." in messages
    assert "Reusing database SPILLAGE_DEMO_DB." in messages
    assert "Reusing table SPILLAGE_DEMO_DB.PUBLIC.LINEITEM." in messages


def test_setup_regenerates_a_missing_table(account):
    # The database exists but an earlier setup was interrupted before the table was built.
    cursor, conn = account(table_exists=False)
    infra.setup(conn, OBJECTS)
    assert any(s.strip().startswith("CREATE TABLE SPILLAGE_DEMO_DB.PUBLIC.LINEITEM AS") for s in cursor.executed)


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


def test_require_returns_the_generation(account):
    cursor, _conn = account(generation="2")
    assert infra.require(cursor, OBJECTS) == "2"


def test_require_assumes_gen2_when_the_account_does_not_say(account):
    cursor, _conn = account(generation=None)
    messages: list[str] = []
    assert infra.require(cursor, OBJECTS, echo=messages.append) == "2"  # the pricier rate keeps the cap safe
    assert "assume Gen2" in messages[0]


def test_require_points_at_setup_when_the_table_is_missing(account):
    cursor, _conn = account(table_exists=False)
    with pytest.raises(ValueError, match="setup didn't finish"):
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
