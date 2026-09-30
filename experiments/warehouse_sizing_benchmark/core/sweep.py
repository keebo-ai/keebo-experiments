"""Run the fixed query across warehouse sizes (Steps 4-9).

``sweep_sizes`` takes an already-open connection — the injection seam — so it
runs against a connection opened by the CLI, a notebook, or a test fake. It needs
the objects ``infra.setup`` creates. No ``click`` here; problems raise plain
``ValueError`` and the CLI turns them into clean messages.

Each query's runtime and spill are read back *live* (seconds of lag), so the
results can be shown as soon as the sweep finishes; ``report.read_report``
reconciles against ``ACCOUNT_USAGE`` later for the billed credits.
"""

from __future__ import annotations

import re
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from common import dedicated, warehouses
from experiments.warehouse_sizing_benchmark.core import infra, queries

Echo = Callable[[str], None]

# Snowflake error 000630: the statement hit its STATEMENT_TIMEOUT_IN_SECONDS.
_TIMEOUT_ERRNO = 630


def _silent(_message: str) -> None:
    """The default progress sink; the CLI passes ``click.echo`` instead."""


@dataclass(frozen=True)
class SizeResult:
    """How the fixed query did on one warehouse size."""

    keyword: str
    label: str
    credits_per_hour: float
    runtimes_s: tuple[float, ...]  # per run, Snowflake's elapsed time (else the client's); run 1 is cold
    gb_spill_local: float | None  # the cold run's spill
    gb_spill_remote: float | None
    timed_out: bool = False  # a run hit the cost cap: its numbers are lower bounds

    @property
    def cold_s(self) -> float:
        return self.runtimes_s[0]

    @property
    def warm_s(self) -> float | None:
        """Median of the warm runs (2+), if there were any."""
        return statistics.median(self.runtimes_s[1:]) if len(self.runtimes_s) > 1 else None

    @property
    def query_credits(self) -> float:
        """The cold query's share of warehouse time: runtime x credits/hr."""
        return round(self.cold_s * self.credits_per_hour / 3600, 5)

    @property
    def billed_credits(self) -> float:
        """What this size added to the bill: its runs back to back, at least the 60s minimum."""
        active_s = max(sum(self.runtimes_s), warehouses.BILLING_MINIMUM_SECONDS)
        return round(active_s * self.credits_per_hour / 3600, 5)


def sweep_sizes(
    conn: Any,
    *,
    objects: infra.BenchmarkObjects,
    table: str | None = None,
    sizes: Sequence[tuple[str, str, int]] = tuple(queries.SIZES),
    runs: int = 3,
    max_credits: float = queries.DEFAULT_MAX_CREDITS,
    run_id: str | None = None,
    echo: Echo = _silent,
    clock: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], None] = time.sleep,
) -> list[SizeResult]:
    """Run the fixed query ``runs`` times on each size (Steps 4-9) and return what each size did.

    ``max_credits`` caps the whole sweep: it's split evenly across its queries and
    enforced as each size's statement timeout. A query that hits it is cancelled
    and reported as timed out rather than failing the sweep.
    """
    if runs < 1:
        raise ValueError(f"runs must be at least 1, got {runs}")
    run_id = run_id or datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    if not re.fullmatch(r"[0-9A-Za-z-]+", run_id):
        raise ValueError(f"run id must be letters, digits, and dashes, got {run_id!r}")

    cur = conn.cursor()
    results: list[SizeResult] = []
    try:
        state = infra.require(cur, objects, table=table, echo=echo)
        timeouts = queries.query_timeouts(
            [keyword for keyword, _, _ in sizes], generation=state.generation, runs=runs, max_credits=max_credits
        )
        echo(f"Run id: {run_id} (how `warehouse-sizing report` finds this run)")
        echo(f"Data: {state.table}")
        echo(
            f"Cost cap: at most {max_credits:g} credits of Gen{state.generation} compute, 60-second minimums "
            f"included ({len(sizes)} sizes x {runs} runs; a query on the largest size stops after "
            f"{min(timeouts.values())}s)."
        )

        # Step 2: point a session variable at the table.
        cur.execute(f"SET lineitem_table = '{state.table}'")
        # Step 3: select the benchmark warehouse (setup created it).
        cur.execute(f"USE WAREHOUSE {objects.warehouse}")
        # Turn off the result cache, else a repeated query returns for free and
        # defeats the measurement.
        cur.execute("ALTER SESSION SET USE_CACHED_RESULT = FALSE")

        # Steps 4-9: the sweep.
        try:
            for keyword, label, _credits in sizes:
                results.append(
                    _run_size(
                        cur,
                        objects,
                        keyword=keyword,
                        label=label,
                        generation=state.generation,
                        runs=runs,
                        run_id=run_id,
                        timeout_s=timeouts[keyword],
                        echo=echo,
                        clock=clock,
                        sleep=sleep,
                    )
                )
        finally:
            _reset_for_idle(cur, objects.warehouse)

        echo(
            "\nSweep complete. ACCOUNT_USAGE lags a few minutes (up to ~45), so wait, then run:  "
            "keebo-experiments warehouse-sizing report"
        )
    finally:
        cur.close()
    return results


def _run_size(
    cur: Any,
    objects: infra.BenchmarkObjects,
    *,
    keyword: str,
    label: str,
    generation: str,
    runs: int,
    run_id: str,
    timeout_s: int,
    echo: Echo,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> SizeResult:
    """Run the query ``runs`` times on one size, cold first, and read back how each went."""
    echo(f"\n=== {label} ({keyword}) ===")
    cur.execute(
        f"ALTER WAREHOUSE {objects.warehouse} SET WAREHOUSE_SIZE = {keyword} STATEMENT_TIMEOUT_IN_SECONDS = {timeout_s}"
    )
    cur.execute(f"ALTER WAREHOUSE {objects.warehouse} RESUME IF SUSPENDED")
    runtimes: list[float] = []
    spill: dict[str, Any] = {}
    timed_out = False
    try:
        for attempt in range(1, runs + 1):
            cur.execute(f"ALTER SESSION SET QUERY_TAG = '{queries.QUERY_TAG_PREFIX}:{run_id}:{keyword}:{attempt}'")
            started = clock()
            try:
                cur.execute(queries.BENCHMARK_QUERY)
                cur.fetchall()  # force full execution
            except Exception as exc:
                if getattr(exc, "errno", None) != _TIMEOUT_ERRNO:
                    raise
                timed_out = True
            client_s = clock() - started
            query_id = getattr(cur, "sfqid", "") or ""
            # Untag the session so the lookups below aren't counted as benchmark queries.
            cur.execute("ALTER SESSION UNSET QUERY_TAG")

            elapsed_ms = _lookup(
                cur,
                queries.ELAPSED_SQL.format(database=objects.database),
                query_id,
                "elapsed_ms",
                sleep=sleep,
                on_error=lambda exc: echo(f"  (couldn't read Snowflake's elapsed time: {exc}; using the client's)"),
            )
            runtime = client_s if elapsed_ms is None else float(elapsed_ms) / 1000
            runtimes.append(round(runtime, 1))
            warmth = "cold" if attempt == 1 else "warm"
            if timed_out:
                echo(f"  run {attempt} ({warmth}): stopped by the cost cap after {runtime:.0f}s  [{query_id}]")
                break  # later runs would hit the cap too; save the budget
            echo(f"  run {attempt} ({warmth}): {runtime:6.1f}s  [{query_id}]")
            if attempt == 1:
                spill = _read_spill(cur, query_id, sleep=sleep, echo=echo)
    finally:
        # SUSPEND stops the billing and clears the local cache, so the next size starts cold.
        dedicated.suspend_quietly(cur, objects.warehouse, echo)

    return SizeResult(
        keyword=keyword,
        label=label,
        credits_per_hour=warehouses.credits_per_hour(keyword, generation),
        runtimes_s=tuple(runtimes),
        gb_spill_local=_gb(spill.get("bytes_local")),
        gb_spill_remote=_gb(spill.get("bytes_remote")),
        timed_out=timed_out,
    )


def _read_spill(cur: Any, query_id: str, *, sleep: Callable[[float], None], echo: Echo) -> dict[str, Any]:
    """The cold run's spill from GET_QUERY_OPERATOR_STATS; empty if it can't be read."""
    stats = _lookup_row(
        cur,
        queries.SPILL_SQL,
        query_id,
        ready="operators",
        sleep=sleep,
        on_error=lambda exc: echo(f"  (couldn't read spill: {exc})"),
    )
    if stats is None:
        echo("  (spill not available yet — `warehouse-sizing report` reads it from ACCOUNT_USAGE later)")
        return {}
    echo(f"  spill: {_gb(stats['bytes_local'])} GB local, {_gb(stats['bytes_remote'])} GB remote")
    return stats


def _lookup(
    cur: Any,
    sql: str,
    query_id: str,
    column: str,
    *,
    sleep: Callable[[float], None],
    on_error: Callable[[Exception], None],
) -> Any:
    row = _lookup_row(cur, sql, query_id, ready=column, sleep=sleep, on_error=on_error)
    return None if row is None else row[column]


def _lookup_row(
    cur: Any,
    sql: str,
    query_id: str,
    *,
    ready: str,
    sleep: Callable[[float], None],
    on_error: Callable[[Exception], None],
    tries: int = 3,
    delay: float = 1.5,
) -> dict[str, Any] | None:
    """One stats row for ``query_id``, once ``ready`` is set, or ``None``.

    Never raises: by now the query has been paid for, so a failed lookup should
    cost the results a number, not the whole sweep. The retries stay within the
    per-query overhead the cost cap allows (queries.OVERHEAD_SECONDS_PER_QUERY).
    """
    try:
        for attempt in range(1, tries + 1):
            cur.execute(sql, (query_id,))
            rows = cur.fetchall()
            columns = [col[0].lower() for col in cur.description]
            row = dict(zip(columns, rows[0], strict=False)) if rows else {}
            if row.get(ready):
                return row
            if attempt < tries:
                sleep(delay)
    except Exception as exc:  # best effort by design
        on_error(exc)
    return None


def _reset_for_idle(cur: Any, warehouse: str) -> None:
    """Leave the warehouse X-Small with setup's timeout, ready for `report` and reruns. Best effort."""
    try:
        cur.execute(
            f"ALTER WAREHOUSE {warehouse} SET WAREHOUSE_SIZE = XSMALL "
            f"STATEMENT_TIMEOUT_IN_SECONDS = {queries.SETUP_TIMEOUT_SECONDS}"
        )
    except Exception:  # never hide the exception that got us here
        pass


def _gb(value: Any) -> float | None:
    return None if value is None else round(float(value) / 1024**3, 2)
