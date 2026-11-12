"""Run every cell of the grid, one fresh warehouse at a time.

``run_grid`` takes an already-open connection (the injection seam), so it runs
against a connection opened by the CLI, a notebook, or a test fake. It needs the
objects ``infra.setup`` creates. No ``click`` here; problems raise plain
``ValueError`` and the CLI turns them into clean messages. Time is injected too
(``clock``, ``sleep``), so tests don't wait.

For each cell it:

1. creates a warehouse for that cell alone (free until it runs a query),
2. tags the session with the cell's query tag and runs the workload, either one
   statement at a time or all at once, never past the cell's wall-clock limit,
3. drops the warehouse, which stops a standard warehouse's billing on the spot.

Runtimes are known as soon as the run ends. What each cell *cost* has to wait
for ``ACCOUNT_USAGE`` (see :mod:`report`): Snowflake doesn't publish a rate for
Adaptive, so only its bill can say.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from common import dedicated, warehouses
from common.snowflake import is_statement_timeout
from experiments.adaptive_compute.core import infra, plan, queries

Echo = Callable[[str], None]


def _silent(_message: str) -> None:
    """The default progress sink; the CLI passes ``click.echo`` instead."""


@dataclass(frozen=True)
class CellRun:
    """What one cell did, as the client saw it."""

    cell: plan.Cell
    warehouse: str
    wall_limit_s: int
    wall_s: float  # first statement submitted to last one finished
    up_s: float  # first statement submitted to the warehouse dropped
    statements: int
    succeeded: int
    capped: int  # stopped by the wall-clock limit, or never started because of it
    failed: int  # failed for any other reason
    worst_case_credits: float  # what the cap counted against the budget

    @property
    def complete(self) -> bool:
        return self.succeeded == self.statements

    @property
    def estimated_credits(self) -> float | None:
        """A standard warehouse's bill from the client's clock (one cluster, 60-second minimum); ``None`` for Adaptive.

        For the concurrent workload on more than one cluster this is a lower
        bound: extra clusters aren't visible from here. The report has the bill.
        """
        if self.cell.config.adaptive:
            return None
        rate = warehouses.credits_per_hour(self.cell.size, self.cell.config.generation or "1")
        return round(max(self.up_s, warehouses.BILLING_MINIMUM_SECONDS) * rate / 3600, 5)


@dataclass(frozen=True)
class RunResult:
    run_id: str
    scale: int
    max_clusters: int
    throughput_multiplier: int
    cells: tuple[CellRun, ...]
    stopped_early: str | None = None  # why the run stopped before the last cell, if it did

    @property
    def worst_case_credits(self) -> float:
        return sum(cell.worst_case_credits for cell in self.cells)


def new_run_id(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%d_%H%M%S")


def run_grid(
    conn: Any,
    *,
    objects: infra.HomeObjects,
    cells: Sequence[plan.Cell],
    scale: int = queries.DEFAULT_SCALE,
    max_credits: float = queries.DEFAULT_MAX_CREDITS,
    max_clusters: int = queries.DEFAULT_MAX_CLUSTERS,
    throughput_multiplier: int = queries.DEFAULT_THROUGHPUT_MULTIPLIER,
    run_id: str | None = None,
    echo: Echo = _silent,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> RunResult:
    """Run ``cells`` in order and return what each did.

    ``max_credits`` caps the run: before each cell, what's left of the budget
    (after every earlier cell's *worst case*, not its estimate) is split across
    the cells still to go, and becomes this cell's wall-clock limit (see
    :func:`plan.wall_limit`). A cell that hits it is cancelled and its numbers
    are lower bounds.
    """
    run_id = queries.validate_run_id(run_id or new_run_id())
    # Raises before anything is created if the cap can't cover every cell.
    plan.summarize(
        cells, max_credits=max_credits, max_clusters=max_clusters, throughput_multiplier=throughput_multiplier
    )
    schema = queries.sample_schema(scale)
    base = queries.base_table(objects.database, scale)

    cur = conn.cursor()
    results: list[CellRun] = []
    stopped: str | None = None
    try:
        infra.require(cur, objects, scale=scale, need_base=any(c.workload is queries.DML for c in cells))
        echo(f"Run id: {run_id} (pass it to `adaptive-compute report --run-id`)")
        echo(f"Data: {schema}. {len(cells)} cells, each on its own new warehouse. Cost cap: {max_credits:g} credits.")
        # Turn off the result cache, else a repeated query returns for free and
        # defeats the measurement.
        cur.execute("ALTER SESSION SET USE_CACHED_RESULT = FALSE")

        spent = 0.0
        introduced: set[str] = set()
        try:
            for index, cell in enumerate(cells):
                if cell.workload.key not in introduced:
                    introduced.add(cell.workload.key)
                    echo(f"\n##### {cell.workload.key}: {cell.workload.does}")
                    echo(f"      Why: {cell.workload.why}")
                wall = plan.wall_limit(
                    cell,
                    budget=max_credits - spent,
                    cells_left=len(cells) - index,
                    max_clusters=max_clusters,
                    throughput_multiplier=throughput_multiplier,
                )
                result = _run_cell(
                    conn,
                    cur,
                    cell,
                    objects=objects,
                    run_id=run_id,
                    schema=schema,
                    base=base,
                    wall_s=wall,
                    max_clusters=max_clusters,
                    throughput_multiplier=throughput_multiplier,
                    echo=echo,
                    clock=clock,
                    sleep=sleep,
                )
                results.append(result)
                spent += result.worst_case_credits
        except ValueError as exc:
            # Keep what's been measured: the cells that ran are paid for.
            if not results:
                raise
            stopped = str(exc)
            echo(f"\nStopped before finishing the grid: {exc}")
    finally:
        with_suppressed(cur, "ALTER SESSION UNSET QUERY_TAG")
        cur.close()
    return RunResult(
        run_id=run_id,
        scale=scale,
        max_clusters=max_clusters,
        throughput_multiplier=throughput_multiplier,
        cells=tuple(results),
        stopped_early=stopped,
    )


def _run_cell(
    conn: Any,
    cur: Any,
    cell: plan.Cell,
    *,
    objects: infra.HomeObjects,
    run_id: str,
    schema: str,
    base: str,
    wall_s: int,
    max_clusters: int,
    throughput_multiplier: int,
    echo: Echo,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> CellRun:
    """Create the cell's warehouse, run its workload, and drop the warehouse whatever happens."""
    warehouse = cell.warehouse(objects.warehouse, run_id)
    table = cell.table(objects.database, run_id)
    statements = queries.workload_statements(cell.workload, schema=schema, table=table, base=base)
    echo(f"\n=== {cell.label} (limit {wall_s}s) ===")

    if dedicated.claim(cur, "WAREHOUSE", warehouse, comment=queries.OWNER_COMMENT) is None:
        try:
            cur.execute(
                queries.create_cell_warehouse_sql(
                    warehouse,
                    config=cell.config,
                    size=cell.size,
                    concurrent=cell.workload.concurrent,
                    max_clusters=max_clusters,
                    throughput_multiplier=throughput_multiplier,
                    timeout_s=wall_s,
                )
            )
        except Exception as exc:  # the connector raises many error types
            raise ValueError(f"couldn't create {warehouse} for {cell.label}: {exc}{_create_hint(cell)}") from exc

    started = clock()
    outcome = _Outcome()
    try:
        for sql in queries.prepare_statements(cell.workload, table=table, base=base):
            cur.execute(sql)
        cur.execute(f"USE WAREHOUSE {warehouse}")
        cur.execute(f"ALTER SESSION SET QUERY_TAG = '{cell.tag(run_id)}'")
        if cell.workload.concurrent:
            outcome = _run_concurrent(
                conn, cur, warehouse, statements, wall_s=wall_s, echo=echo, clock=clock, sleep=sleep
            )
        else:
            outcome = _run_sequential(cur, statements, wall_s=wall_s, echo=echo, clock=clock)
    finally:
        with_suppressed(cur, "ALTER SESSION UNSET QUERY_TAG")
        with_suppressed(cur, "ALTER SESSION UNSET STATEMENT_TIMEOUT_IN_SECONDS")
        with_suppressed(cur, "ALTER SESSION UNSET STATEMENT_QUEUED_TIMEOUT_IN_SECONDS")
        # Dropping stops a standard warehouse's billing at once, and the cell
        # never needs it again. Its bill stays in ACCOUNT_USAGE by name.
        try:
            dedicated.drop(cur, "WAREHOUSE", warehouse, comment=queries.OWNER_COMMENT)
        except Exception as exc:  # best effort: it auto-suspends, and cleanup drops it
            echo(f"  (couldn't drop {warehouse}: {exc}; `cleanup` will)")
        up_s = clock() - started
        for sql in queries.teardown_statements(cell.workload, table=table):
            with_suppressed(cur, sql)

    wall = outcome.wall_s if outcome.wall_s is not None else up_s
    result = CellRun(
        cell=cell,
        warehouse=warehouse,
        wall_limit_s=wall_s,
        wall_s=round(wall, 1),
        up_s=round(up_s, 1),
        statements=len(statements),
        succeeded=outcome.succeeded,
        capped=outcome.capped,
        failed=outcome.failed,
        worst_case_credits=plan.worst_case_credits(
            cell, up_s, max_clusters=max_clusters, throughput_multiplier=throughput_multiplier
        ),
    )
    flags = []
    if result.capped:
        flags.append(f"{result.capped} stopped by the limit")
    if result.failed:
        flags.append(f"{result.failed} failed")
    echo(
        f"  {result.succeeded}/{result.statements} statements in {result.wall_s:.1f}s"
        + (f" ({', '.join(flags)})" if flags else "")
    )
    return result


@dataclass
class _Outcome:
    succeeded: int = 0
    capped: int = 0
    failed: int = 0
    wall_s: float | None = None


def _run_sequential(
    cur: Any,
    statements: list[tuple[str, str]],
    *,
    wall_s: int,
    echo: Echo,
    clock: Callable[[], float],
) -> _Outcome:
    """Run the statements one after another. Each may only use what's left of the cell's limit."""
    outcome = _Outcome()
    budget = wall_s - queries.OVERHEAD_SECONDS
    started = clock()
    for position, (name, sql) in enumerate(statements):
        remaining = int(budget - (clock() - started))
        if remaining < 1:
            outcome.capped += len(statements) - position
            echo(f"  the limit is used up; skipping the last {len(statements) - position} statement(s)")
            break
        cur.execute(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {remaining}")
        query_started = clock()
        try:
            cur.execute(sql)
            cur.fetchall()  # force full execution
            outcome.succeeded += 1
            echo(f"  {name}: {clock() - query_started:6.1f}s")
        except Exception as exc:
            if is_statement_timeout(exc):
                outcome.capped += 1
                echo(f"  {name}: stopped by the limit after {clock() - query_started:.0f}s")
            else:
                # The same SQL runs on every warehouse, so a failure here is
                # worth seeing, but not worth losing the rest of the cell for.
                outcome.failed += 1
                echo(f"  {name}: FAILED: {exc}")
    outcome.wall_s = clock() - started
    return outcome


def _run_concurrent(
    conn: Any,
    cur: Any,
    warehouse: str,
    statements: list[tuple[str, str]],
    *,
    wall_s: int,
    echo: Echo,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> _Outcome:
    """Submit every statement at once, wait for them all, and cancel whatever's left at the limit.

    Each is submitted with ``execute_async`` on its own cursor of the same
    connection, so they run concurrently server-side and share the session's
    query tag and settings. The session timeouts are a backstop; the deadline
    here is what holds the cell to its limit.
    """
    budget = wall_s - queries.OVERHEAD_SECONDS
    cur.execute(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {budget}")
    cur.execute(f"ALTER SESSION SET STATEMENT_QUEUED_TIMEOUT_IN_SECONDS = {budget}")
    started = clock()
    query_ids: list[str] = []
    for _name, sql in statements:
        query_cursor = conn.cursor()
        try:
            query_cursor.execute_async(sql)
            query_ids.append(query_cursor.sfqid)
        finally:
            # Closing a cursor doesn't cancel the async query it submitted.
            query_cursor.close()
    echo(f"  submitted {len(query_ids)} statements at once")

    pending = list(query_ids)
    statuses: dict[str, str] = {}
    hit_limit = False
    while pending:
        still: list[str] = []
        for query_id in pending:
            status = conn.get_query_status(query_id)
            if conn.is_still_running(status):
                still.append(query_id)
            else:
                statuses[query_id] = _status_name(status)
        pending = still
        if not pending:
            break
        if clock() - started >= budget:
            hit_limit = True
            echo(f"  the limit is up with {len(pending)} still running; cancelling them")
            _cancel(cur, warehouse, pending)
            break
        sleep(queries.POLL_SECONDS)
    elapsed = clock() - started

    outcome = _Outcome(wall_s=elapsed)
    outcome.succeeded = sum(1 for status in statuses.values() if status == "SUCCESS")
    unfinished = len(query_ids) - outcome.succeeded
    # A query the limit cancelled, or the session timeout stopped, ends as a
    # failure too. Past the limit there's no telling them apart, so count them as capped.
    if hit_limit or elapsed >= budget:
        outcome.capped = unfinished
    else:
        outcome.failed = unfinished
        for query_id, status in statuses.items():
            if status != "SUCCESS":
                echo(f"  {query_id}: {status}")
    echo(f"  all done after {elapsed:.1f}s")
    return outcome


def _status_name(status: Any) -> str:
    """The connector's QueryStatus enum, or a plain string (as the test fakes return), by name."""
    return str(getattr(status, "name", status))


def _cancel(cur: Any, warehouse: str, query_ids: list[str]) -> None:
    """Stop every query still running on the cell's warehouse."""
    try:
        cur.execute(f"ALTER WAREHOUSE {warehouse} ABORT ALL QUERIES")
    except Exception:  # not every warehouse type takes it; cancel them one by one
        for query_id in query_ids:
            with_suppressed(cur, "SELECT SYSTEM$CANCEL_QUERY(%s)", (query_id,))


def with_suppressed(cur: Any, sql: str, params: tuple[Any, ...] | None = None) -> None:
    """Run ``sql``, ignoring errors, for ``finally`` blocks that mustn't hide the real exception."""
    try:
        if params is None:
            cur.execute(sql)
        else:
            cur.execute(sql, params)
    except Exception:  # best effort by design
        pass


def _create_hint(cell: plan.Cell) -> str:
    if cell.config.adaptive:
        return (
            "\nAdaptive warehouses need the Enterprise edition and a region that has them. "
            "`setup` checks; to measure only the standard ones, pass --config gen1 --config gen2."
        )
    if cell.config.generation == "2":
        return "\nGen2 isn't in every region. To leave it out, pass --config gen1 --config adaptive."
    if cell.workload.concurrent:
        return "\nMulti-cluster warehouses need the Enterprise edition. Pass --max-clusters 1 to use one cluster."
    return ""
