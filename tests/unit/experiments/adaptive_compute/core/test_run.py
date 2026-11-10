"""Running the grid: one fresh warehouse per cell, held to its wall-clock limit."""

from __future__ import annotations

import pytest

from experiments.adaptive_compute.core import plan, queries, run

from ..conftest import OBJECTS, OURS, RUN_ID


def _cells(*workloads, configs=queries.CONFIGS, sizes=("XSMALL",)):
    return plan.build_plan(configs=configs, sizes=list(sizes), workloads=list(workloads or [queries.SINGLE]))


def _run(conn, cells, clock, **kwargs):
    return run.run_grid(conn, objects=OBJECTS, cells=cells, run_id=RUN_ID, clock=clock, sleep=clock.sleep, **kwargs)


def test_each_cell_gets_its_own_warehouse_which_is_dropped_after(account, clock):
    cursor, conn, fake = account()

    result = _run(conn, _cells(), clock)

    names = [c.warehouse for c in result.cells]
    assert names == [f"{OBJECTS.warehouse}_{RUN_ID}_{code}_XSMALL_ONE_1" for code in ("G1", "G2", "AD")]
    for name in names:
        assert any(s.startswith(f"CREATE WAREHOUSE {name} WITH") for s in cursor.executed)
        assert f"DROP WAREHOUSE IF EXISTS {name}" in cursor.executed
    assert set(fake.warehouses) == {OBJECTS.warehouse}
    assert all(c.complete for c in result.cells)


def test_the_session_is_tagged_per_cell_with_the_result_cache_off(account, clock):
    cursor, conn, _fake = account()

    _run(conn, _cells(configs=[queries.ADAPTIVE]), clock)

    sql = cursor.executed
    assert "ALTER SESSION SET USE_CACHED_RESULT = FALSE" in sql
    tag = f"ALTER SESSION SET QUERY_TAG = 'adcomp:{RUN_ID}:single:XSMALL:adaptive:1'"
    use = f"USE WAREHOUSE {OBJECTS.warehouse}_{RUN_ID}_AD_XSMALL_ONE_1"
    q1 = next(i for i, s in enumerate(sql) if "FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF10.lineitem" in s)
    assert sql.index(use) < sql.index(tag) < q1
    assert "ALTER SESSION UNSET QUERY_TAG" in sql[q1:]


def test_each_statement_only_gets_what_is_left_of_the_limit(account, clock):
    cursor, conn, _fake = account()

    result = _run(conn, _cells(queries.SEQUENTIAL, configs=[queries.GEN1]), clock)

    timeouts = [
        int(s.rsplit("=", 1)[1]) for s in cursor.executed if s.startswith("ALTER SESSION SET STATEMENT_TIMEOUT")
    ]
    assert len(timeouts) == 22
    assert timeouts == sorted(timeouts, reverse=True)
    assert timeouts[0] <= result.cells[0].wall_limit_s - queries.OVERHEAD_SECONDS


def test_a_statement_that_hits_the_limit_is_capped_and_the_rest_are_skipped(account, clock, snowflake_timeout):
    clock.step = 30  # every clock read is 30 seconds later
    _cursor, conn, _fake = account(fail={"GROUP BY l_returnflag": [snowflake_timeout()]})

    result = _run(conn, _cells(queries.SEQUENTIAL, configs=[queries.GEN1]), clock, max_credits=0.5)

    cell = result.cells[0]
    assert cell.capped >= 1 and cell.capped + cell.succeeded == 22
    assert cell.failed == 0


def test_a_failing_statement_is_counted_and_the_cell_carries_on(account, clock):
    _cursor, conn, _fake = account(fail={"GROUP BY l_returnflag": [RuntimeError("SQL compilation error")]})
    messages: list[str] = []

    result = _run(conn, _cells(queries.SEQUENTIAL, configs=[queries.GEN1]), clock, echo=messages.append)

    assert (result.cells[0].failed, result.cells[0].succeeded) == (1, 21)
    assert any("q01: FAILED: SQL compilation error" in m for m in messages)


def test_the_concurrent_workload_submits_everything_at_once(account, clock):
    cursor, conn, _fake = account()

    result = _run(conn, _cells(queries.CONCURRENT, configs=[queries.GEN1]), clock)

    assert sum("FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF10" in s for s in cursor.executed) == 22
    assert result.cells[0].succeeded == 22
    assert "STATEMENT_QUEUED_TIMEOUT_IN_SECONDS" in " ".join(cursor.executed)
    assert any("MAX_CLUSTER_COUNT = 2" in s for s in cursor.executed)


def test_the_concurrent_workload_is_cancelled_at_its_limit(account, clock):
    cursor, conn, _fake = account()
    # Every query is still running however often it's asked.
    original = conn.get_query_status
    conn.get_query_status = lambda query_id: "RUNNING" if query_id != "fake-query-1" else original(query_id)

    result = _run(conn, _cells(queries.CONCURRENT, configs=[queries.GEN1]), clock, max_credits=0.5)

    cell = result.cells[0]
    assert f"ALTER WAREHOUSE {cell.warehouse} ABORT ALL QUERIES" in cursor.executed
    assert (cell.succeeded, cell.capped, cell.failed) == (1, 21, 0)
    assert cell.wall_s <= cell.wall_limit_s


def test_the_dml_workload_clones_first_and_drops_its_table_after(account, clock):
    cursor, conn, _fake = account()

    result = _run(conn, _cells(queries.DML, configs=[queries.GEN2]), clock)

    table = result.cells[0].cell.table(OBJECTS.database, RUN_ID)
    sql = cursor.executed
    clone = sql.index(f"CREATE TABLE {table} CLONE {OBJECTS.database}.PUBLIC.LINEITEM_SF10")
    delete = next(i for i, s in enumerate(sql) if s.startswith(f"DELETE FROM {table}"))
    assert clone < delete < sql.index(f"DROP TABLE IF EXISTS {table}")


def test_the_warehouse_is_dropped_even_when_the_cell_blows_up(account, clock):
    cursor, conn, fake = account(fail={"USE WAREHOUSE": [RuntimeError("network")]})
    with pytest.raises(RuntimeError, match="network"):
        _run(conn, _cells(configs=[queries.GEN1]), clock)
    assert set(fake.warehouses) == {OBJECTS.warehouse}


def test_a_warehouse_that_cant_be_created_stops_the_run_with_a_hint(account, clock):
    _cursor, conn, _fake = account(fail={"WAREHOUSE_TYPE = 'ADAPTIVE'": [RuntimeError("Unsupported feature")]})
    with pytest.raises(ValueError, match="Enterprise edition"):
        _run(conn, _cells(configs=[queries.ADAPTIVE]), clock)


def test_a_failure_after_some_cells_keeps_what_was_measured(account, clock):
    _cursor, conn, _fake = account(fail={"WAREHOUSE_TYPE = 'ADAPTIVE'": [RuntimeError("Unsupported feature")]})

    result = _run(conn, _cells(), clock)

    assert [c.cell.config.key for c in result.cells] == ["gen1", "gen2"]
    assert "Enterprise edition" in result.stopped_early


def test_the_cap_is_checked_before_anything_is_created(account, clock):
    cursor, conn, _fake = account()
    with pytest.raises(ValueError, match="Raise --max-credits"):
        _run(conn, _cells(queries.CONCURRENT, sizes=("MEDIUM",)), clock, max_credits=0.5, max_clusters=4)
    assert not any(s.startswith("CREATE") for s in cursor.executed)


def test_someone_elses_warehouse_with_a_cell_name_is_refused(account, clock):
    name = f"{OBJECTS.warehouse}_{RUN_ID}_G1_XSMALL_ONE_1"
    _cursor, conn, fake = account(warehouses={OBJECTS.warehouse: OURS, name: "production"})
    with pytest.raises(ValueError, match="didn't create it"):
        _run(conn, _cells(configs=[queries.GEN1]), clock)
    assert fake.warehouses[name] == "production"


def test_estimates_cover_standard_warehouses_only():
    cell = run.CellRun(
        cell=plan.Cell(queries.SINGLE, "SMALL", queries.GEN1),
        warehouse="W",
        wall_limit_s=100,
        wall_s=5,
        up_s=8,
        statements=1,
        succeeded=1,
        capped=0,
        failed=0,
        worst_case_credits=0.03,
    )
    assert cell.estimated_credits == pytest.approx(2 * 60 / 3600, abs=1e-5)  # the 60-second minimum
    adaptive = run.CellRun(**{**cell.__dict__, "cell": plan.Cell(queries.SINGLE, "SMALL", queries.ADAPTIVE)})
    assert adaptive.estimated_credits is None


# --------------------------------------------------------------------------- #
# The monster workloads
# --------------------------------------------------------------------------- #
def _split_statements(cursor):
    return [s for s in cursor.executed if s.startswith("CREATE TEMPORARY TABLE") or "HASH_AGG" in s]


def test_monster_runs_one_long_statement_and_keeps_its_answer(account, clock):
    cursor, conn, fake = account()

    result = _run(conn, _cells(queries.MONSTER, configs=[queries.GEN1]), clock, fanout=4)

    (statement,) = [s for s in cursor.executed if "HASH_AGG" in s]
    assert statement.startswith("WITH b01_a AS (") and "TPCH_SF10.lineitem" in statement
    cell = result.cells[0]
    assert (cell.statements, cell.succeeded, cell.answer) == (1, 1, fake.answer)
    assert not any("MAX_CLUSTER_COUNT" in s for s in cursor.executed)


def test_monster_split_writes_every_branch_before_combining_them_then_drops_them(account, clock):
    cursor, conn, _fake = account()
    messages: list[str] = []

    result = _run(conn, _cells(queries.MONSTER_SPLIT, configs=[queries.GEN2]), clock, fanout=4, echo=messages.append)

    cell = result.cells[0]
    table = cell.cell.table(OBJECTS.database, RUN_ID)
    statements = _split_statements(cursor)
    assert [s.split(" AS")[0] for s in statements[:4]] == [f"CREATE TEMPORARY TABLE {table}_B0{i}" for i in range(1, 5)]
    assert statements[4].startswith("WITH c1_01 AS (")
    assert (cell.statements, cell.succeeded, cell.failed, cell.capped) == (5, 5, 0, 0)
    assert cell.answer is not None
    assert any("stage 1/2: submitted 4 statements at once" in m for m in messages)
    drops = [f"DROP TABLE IF EXISTS {table}_B0{i}" for i in range(1, 5)]
    assert all(cursor.executed.index(d) > cursor.executed.index(statements[4]) for d in drops)
    # One cluster by default: a comparison inside one warehouse.
    assert not any("MAX_CLUSTER_COUNT" in s for s in cursor.executed)


def test_monster_split_can_have_more_clusters(account, clock):
    cursor, conn, _fake = account()
    _run(conn, _cells(queries.MONSTER_SPLIT, configs=[queries.GEN1]), clock, fanout=2, split_max_clusters=3)
    assert any("MAX_CLUSTER_COUNT = 3" in s for s in cursor.executed)


def test_a_failed_branch_skips_the_combine_and_the_tables_are_still_dropped(account, clock):
    cursor, conn, _fake = account()
    original = conn.get_query_status
    conn.get_query_status = lambda query_id: "FAILED_WITH_ERROR" if query_id == "fake-query-2" else original(query_id)
    messages: list[str] = []

    result = _run(conn, _cells(queries.MONSTER_SPLIT, configs=[queries.GEN1]), clock, fanout=3, echo=messages.append)

    cell = result.cells[0]
    assert (cell.succeeded, cell.failed, cell.capped) == (2, 2, 0)
    assert not any("HASH_AGG" in s and not s.startswith("CREATE") for s in cursor.executed)
    assert cell.answer is None
    assert any("skipping the last 1 statement(s), which need what failed" in m for m in messages)
    table = cell.cell.table(OBJECTS.database, RUN_ID)
    assert f"DROP TABLE IF EXISTS {table}_B03" in cursor.executed


def test_the_limit_covers_every_stage(account, clock):
    cursor, conn, _fake = account()
    conn.get_query_status = lambda query_id: "RUNNING"

    result = _run(conn, _cells(queries.MONSTER_SPLIT, configs=[queries.GEN1]), clock, fanout=3, max_credits=0.5)

    cell = result.cells[0]
    assert f"ALTER WAREHOUSE {cell.warehouse} ABORT ALL QUERIES" in cursor.executed
    assert (cell.succeeded, cell.capped, cell.failed) == (0, 4, 0)
    assert cell.wall_s <= cell.wall_limit_s


def test_a_later_stage_only_gets_what_is_left_of_the_limit(account, clock):
    cursor, conn, _fake = account()

    result = _run(conn, _cells(queries.MONSTER_SPLIT, configs=[queries.GEN1]), clock, fanout=2)

    timeouts = [
        int(s.rsplit("=", 1)[1]) for s in cursor.executed if s.startswith("ALTER SESSION SET STATEMENT_TIMEOUT")
    ]
    assert len(timeouts) == 2
    assert timeouts[1] < timeouts[0] == result.cells[0].wall_limit_s - queries.OVERHEAD_SECONDS


def test_every_monster_cell_should_return_the_same_answer(account, clock):
    _cursor, conn, _fake = account()
    cells = _cells(queries.MONSTER, queries.MONSTER_SPLIT, configs=[queries.GEN1, queries.ADAPTIVE])

    result = _run(conn, cells, clock, fanout=2)

    assert result.mismatched_answers == ()


def test_a_monster_cell_with_a_different_answer_is_called_out(account, clock):
    _cursor, conn, _fake = account(split_answer=(25, 999, 1))
    messages: list[str] = []
    cells = _cells(queries.MONSTER, queries.MONSTER_SPLIT, configs=[queries.GEN1, queries.ADAPTIVE])

    result = _run(conn, cells, clock, fanout=2, max_credits=20, echo=messages.append)

    assert len(result.mismatched_answers) == 2
    assert all(label.startswith("monster-split") for label in result.mismatched_answers)
    assert any(m.startswith("\nWARNING: the monster cells didn't all return the same answer") for m in messages)


def test_a_monster_cell_gets_a_bigger_share_of_the_budget(account, clock):
    _cursor, conn, _fake = account()

    result = _run(conn, _cells(queries.SINGLE, queries.MONSTER, configs=[queries.GEN1]), clock, fanout=2)

    single, monster_cell = result.cells
    assert monster_cell.wall_limit_s > single.wall_limit_s


def test_a_bad_fanout_is_refused_before_anything_is_created(account, clock):
    cursor, conn, _fake = account()
    with pytest.raises(ValueError, match="fanout"):
        _run(conn, _cells(queries.MONSTER), clock, fanout=1)
    assert not any(s.startswith("CREATE") for s in cursor.executed)
