"""Setting up, checking, and cleaning up the experiment's own objects."""

from __future__ import annotations

import pytest

from experiments.adaptive_compute.core import infra, queries

from ..conftest import OBJECTS, OURS


def _new_account(account, **settings):
    return account(warehouses={}, databases={}, base_table=False, **settings)


def test_setup_creates_the_home_objects_and_the_sorted_copy(account):
    cursor, conn, fake = _new_account(account)
    messages: list[str] = []

    infra.setup(conn, OBJECTS, scale=10, echo=messages.append)

    sql = cursor.executed
    assert any(
        s.startswith(f"CREATE WAREHOUSE {OBJECTS.warehouse} WAREHOUSE_SIZE = XSMALL GENERATION = '1'") for s in sql
    )
    assert any(s.startswith(f"CREATE TRANSIENT DATABASE {OBJECTS.database}") for s in sql)
    copy = next(s for s in sql if s.startswith("CREATE TABLE"))
    assert copy == (
        f"CREATE TABLE {OBJECTS.database}.PUBLIC.LINEITEM_SF10 AS "
        "SELECT * FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF10.LINEITEM ORDER BY l_shipdate"
    )
    # The copy runs on a Small with its own timeout, then the warehouse goes back to idle.
    resize = sql.index(
        f"ALTER WAREHOUSE {OBJECTS.warehouse} SET WAREHOUSE_SIZE = SMALL STATEMENT_TIMEOUT_IN_SECONDS = 900"
    )
    assert resize < sql.index(copy)
    assert any("SET WAREHOUSE_SIZE = XSMALL" in s for s in sql[sql.index(copy) :])
    assert f"ALTER WAREHOUSE {OBJECTS.warehouse} SUSPEND" in sql
    assert set(fake.warehouses) == {OBJECTS.warehouse}  # the probes were dropped again
    assert "Ready." in messages[-1]


def test_setup_checks_gen2_and_adaptive_for_free(account):
    cursor, conn, _fake = _new_account(account)
    messages: list[str] = []

    infra.setup(conn, OBJECTS, echo=messages.append)

    probes = [s for s in cursor.executed if "_PROBE_" in s and s.startswith(("CREATE", "DROP"))]
    assert any("PROBE_G2 WAREHOUSE_SIZE = XSMALL GENERATION = '2' INITIALLY_SUSPENDED = TRUE" in s for s in probes)
    assert any("PROBE_AD WITH WAREHOUSE_TYPE = 'ADAPTIVE'" in s for s in probes)
    assert sum(s.startswith("DROP WAREHOUSE") for s in probes) == 2
    assert "  This account can create Adaptive warehouses." in messages


def test_setup_says_how_to_go_on_when_adaptive_isnt_available(account):
    _cursor, conn, _fake = _new_account(
        account, fail={"WAREHOUSE_TYPE = 'ADAPTIVE'": [RuntimeError("Unsupported feature 'ADAPTIVE'")]}
    )
    messages: list[str] = []

    infra.setup(conn, OBJECTS, echo=messages.append)

    note = next(m for m in messages if "can't create Adaptive" in m)
    assert "Enterprise edition" in note and "--config gen1 --config gen2" in note


def test_setup_reuses_what_exists(account):
    cursor, conn, _fake = account()
    messages: list[str] = []

    infra.setup(conn, OBJECTS, echo=messages.append)

    assert not any(s.startswith(("CREATE TABLE", "CREATE TRANSIENT")) for s in cursor.executed)
    assert f"Reusing warehouse {OBJECTS.warehouse}." in messages


def test_setup_needs_the_sample_data(account):
    cursor, conn, _fake = _new_account(account, sample_data=False)
    with pytest.raises(ValueError, match="SNOWFLAKE_SAMPLE_DATA share"):
        infra.setup(conn, OBJECTS)
    assert not any(s.startswith("CREATE") for s in cursor.executed)


def test_setup_wont_touch_someone_elses_warehouse(account):
    cursor, conn, _fake = account(warehouses={OBJECTS.warehouse: "production"}, databases={})
    with pytest.raises(ValueError, match="didn't create it"):
        infra.setup(conn, OBJECTS)
    assert not any(s.startswith("CREATE") for s in cursor.executed)


def test_setup_wont_copy_sf1000(account):
    _cursor, conn, _fake = account()
    with pytest.raises(ValueError, match="several credits"):
        infra.setup(conn, OBJECTS, scale=1000)


def test_require_needs_the_base_copy_only_for_dml(account):
    cursor, _conn, _fake = account(base_table=False)
    infra.require(cursor, OBJECTS, scale=10, need_base=False)
    with pytest.raises(ValueError, match="dml workload clones it"):
        infra.require(cursor, OBJECTS, scale=10, need_base=True)


def test_require_needs_setup(account):
    cursor, _conn, _fake = account(warehouses={})
    with pytest.raises(ValueError, match="Run `keebo-experiments adaptive-compute setup` first"):
        infra.require(cursor, OBJECTS, scale=10, need_base=False)


def test_cleanup_drops_our_leftovers_and_nothing_else(account):
    leftover = f"{OBJECTS.cell_prefix}20261014_143318_G1_SMALL_SEQ_1"
    lookalike = f"{OBJECTS.cell_prefix}SOMEONE_ELSES"
    cursor, conn, fake = account(
        warehouses={OBJECTS.warehouse: OURS, leftover: OURS, lookalike: "production", "OTHER_WH": "production"}
    )
    messages: list[str] = []

    infra.cleanup(conn, OBJECTS, echo=messages.append)

    assert set(fake.warehouses) == {lookalike, "OTHER_WH"}
    assert fake.databases == {}
    assert f"DROP WAREHOUSE IF EXISTS {lookalike}" not in cursor.executed
    assert f"Dropped leftover warehouse {leftover}." in messages


def test_cleanup_is_safe_to_rerun(account):
    _cursor, conn, _fake = account(warehouses={}, databases={})
    messages: list[str] = []
    infra.cleanup(conn, OBJECTS, echo=messages.append)
    assert messages == [
        f"No warehouse {OBJECTS.warehouse} to drop.",
        f"No database {OBJECTS.database} to drop.",
        "Checked: nothing the experiment created is left on the account.",
    ]


def test_cleanup_lists_objects_it_made_under_other_names_without_dropping_them(account):
    _cursor, conn, fake = account(
        warehouses={OBJECTS.warehouse: OURS, "OLD_SETUP_WH": OURS, "PROD_WH": "production"},
        databases={OBJECTS.database: OURS, "OLD_SETUP_DB": OURS},
    )
    messages: list[str] = []

    remaining = infra.cleanup(conn, OBJECTS, echo=messages.append)

    assert remaining == ["warehouse OLD_SETUP_WH", "database OLD_SETUP_DB"]
    assert set(fake.warehouses) == {"OLD_SETUP_WH", "PROD_WH"}
    assert "Rerun with --everything to drop them too." in messages
    assert messages[-1] == "Still on the account: warehouse OLD_SETUP_WH, database OLD_SETUP_DB."


def test_cleanup_everything_drops_every_marked_object_and_nothing_else(account):
    _cursor, conn, fake = account(
        warehouses={OBJECTS.warehouse: OURS, "OLD_SETUP_WH": OURS, "PROD_WH": "production"},
        databases={OBJECTS.database: OURS, "OLD_SETUP_DB": OURS, "PROD_DB": "production"},
    )
    messages: list[str] = []

    remaining = infra.cleanup(conn, OBJECTS, everything=True, echo=messages.append)

    assert remaining == []
    assert fake.warehouses == {"PROD_WH": "production"}
    assert fake.databases == {"PROD_DB": "production"}
    assert messages[-1] == "Checked: nothing the experiment created is left on the account."


def test_the_owner_comment_is_the_experiments_own():
    assert queries.OWNER_COMMENT.startswith("Keebo adaptive-compute")
