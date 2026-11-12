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
