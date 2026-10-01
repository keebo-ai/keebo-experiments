"""Run the fixed query on each warehouse size (Steps 2-9).

``sweep_sizes`` takes an already-open connection (the injection seam), so it runs
against a connection opened by the CLI, a notebook, or a test fake. It needs the
objects ``infra.setup`` creates. No ``click`` here; problems raise plain
``ValueError`` and the CLI turns them into clean messages.

Each query's runtime and spill are read back within seconds, so the results can
be shown as soon as the sweep finishes; ``report.read_report`` reads the billed
numbers from ``ACCOUNT_USAGE`` later.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from common import dedicated, warehouses
from common.snowflake import is_statement_timeout, rows
from experiments.warehouse_sizing_benchmark.core import infra, queries

Echo = Callable[[str], None]

# How often to look for a query's stats before giving up. Together they stay
# inside the per-query allowance the cost cap keeps (queries.OVERHEAD_SECONDS_PER_QUERY).
LOOKUP_TRIES = 3
LOOKUP_DELAY_SECONDS = 1.5


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
    capped_runs: tuple[int, ...] = ()  # runs (1 = cold) the cost cap stopped; their numbers are lower bounds
    billed_s: float | None = None  # how long the size was up, from resume to suspend

    @property
    def cold_s(self) -> float:
        return self.runtimes_s[0]

    @property
    def warm_s(self) -> float | None:
        """Median of the warm runs (2+), if there were any."""
        return statistics.median(self.runtimes_s[1:]) if len(self.runtimes_s) > 1 else None

    @property
    def cold_capped(self) -> bool:
        return 1 in self.capped_runs

    @property
    def warm_capped(self) -> bool:
        return any(run > 1 for run in self.capped_runs)

    @property
    def query_credits(self) -> float:
        """What one query costs on a warehouse that's already running: the median run x credits/hr."""
        return round(statistics.median(self.runtimes_s) * self.credits_per_hour / 3600, 5)

    @property
    def billed_credits(self) -> float:
        """What this size added to the bill: its time up, at least the 60-second minimum."""
        up_s = max(self.billed_s or 0, sum(self.runtimes_s), warehouses.BILLING_MINIMUM_SECONDS)
        return round(up_s * self.credits_per_hour / 3600, 5)


def query_timeouts(keywords: Sequence[str], *, generation: str, runs: int, max_credits: float) -> dict[str, int]:
    """Each size's per-query timeout, so the whole run is designed to stay within ``max_credits``.

    A size bills the greater of 60 seconds and the time it's up, so each size is
    reserved its minimum first, and the rest of the budget is shared evenly
    across the sizes. A size's queries may then use the 60 seconds it pays for
    anyway plus its share, split across its runs, less a small allowance per
    query for the tag and stats statements around it. That becomes the size's
    STATEMENT_TIMEOUT_IN_SECONDS, and Snowflake cancels anything longer.
    Cloud-services credits, normally waived, aren't counted.
    """
    rates = {keyword: warehouses.credits_per_hour(keyword, generation) for keyword in keywords}
    minimum_s = warehouses.BILLING_MINIMUM_SECONDS
    minimums = sum(rate * minimum_s / 3600 for rate in rates.values())
    budget = max_credits - minimums
    if budget <= 0:
        raise ValueError(
            f"--max-credits {max_credits:g} doesn't cover Snowflake's 60-second minimum for these sizes "
            f"({minimums:.2f} credits). Raise --max-credits or pick fewer --size."
        )
    share = budget / len(rates)
    timeouts = {
        keyword: int((minimum_s + share * 3600 / rate) / runs - queries.OVERHEAD_SECONDS_PER_QUERY)
        for keyword, rate in rates.items()
    }
    tightest = min(timeouts, key=timeouts.get)
    if timeouts[tightest] < queries.MIN_TIMEOUT_SECONDS:
        raise ValueError(
            f"--max-credits {max_credits:g} gives each {warehouses.SIZE_LABEL[tightest]} query only "
            f"{timeouts[tightest]}s. Raise --max-credits, or pick fewer --size / --runs."
        )
    return timeouts


def sweep_sizes(
    conn: Any,
    *,
    objects: infra.BenchmarkObjects,
    table: str | None = None,
    sizes: Sequence[str] = warehouses.SIZE_KEYWORDS,
    runs: int = 3,
    max_credits: float = queries.DEFAULT_MAX_CREDITS,
    run_id: str | None = None,
    echo: Echo = _silent,
    clock: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], None] = time.sleep,
) -> list[SizeResult]:
    """Run the fixed query ``runs`` times on each size (Steps 2-9) and return what each size did.

    ``max_credits`` caps the run through per-query statement timeouts (see
    :func:`query_timeouts`). A query that hits one is cancelled and reported as
    a lower bound rather than failing the sweep.
    """
    if runs < 1:
        raise ValueError(f"runs must be at least 1, got {runs}")
    run_id = queries.validate_run_id(run_id or datetime.now(UTC).strftime("%Y%m%d-%H%M%S"))

    cur = conn.cursor()
    results: list[SizeResult] = []
    try:
        state = infra.require(cur, objects, table=table, echo=echo)
        timeouts = query_timeouts(sizes, generation=state.generation, runs=runs, max_credits=max_credits)
        largest = max(sizes, key=lambda keyword: warehouses.CREDITS_PER_HOUR[keyword])
        echo(f"Run id: {run_id} (pass it to `warehouse-sizing report --run-id`)")
        echo(f"Data: {state.table}")
        echo(
            f"Cost cap: {max_credits:g} credits at the Gen{state.generation} rate, including each size's "
            f"60-second minimum. Each {warehouses.SIZE_LABEL[largest]} query stops after {timeouts[largest]}s "
            "(longer on smaller sizes)."
        )

        # Step 2: point a session variable at the table.
        cur.execute(f"SET lineitem_table = '{state.table}'")
        # Step 3: select the benchmark warehouse (setup created it).
        cur.execute(f"USE WAREHOUSE {objects.warehouse}")
        # Turn off the result cache, else a repeated query returns for free and
        # defeats the measurement.
        cur.execute("ALTER SESSION SET USE_CACHED_RESULT = FALSE")
        # Start suspended, so even the first size resizes while idle and runs cold.
        dedicated.suspend_quietly(cur, objects.warehouse)

        # Steps 4-9: the sweep.
        try:
            for keyword in sizes:
                results.append(
                    _run_size(
                        cur,
                        objects,
                        keyword=keyword,
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
            infra.reset_to_idle(cur, objects.warehouse)
    finally:
        cur.close()
    return results


def _run_size(
    cur: Any,
    objects: infra.BenchmarkObjects,
    *,
    keyword: str,
    generation: str,
    runs: int,
    run_id: str,
    timeout_s: int,
    echo: Echo,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> SizeResult:
    """Run the query ``runs`` times on one size, cold first, and read back how it went."""
    label = warehouses.SIZE_LABEL[keyword]
    echo(f"\n=== {label} ({keyword}) ===")
    cur.execute(
        f"ALTER WAREHOUSE {objects.warehouse} SET WAREHOUSE_SIZE = {keyword} STATEMENT_TIMEOUT_IN_SECONDS = {timeout_s}"
    )
    resumed = clock()
    cur.execute(f"ALTER WAREHOUSE {objects.warehouse} RESUME IF SUSPENDED")
    runtimes: list[float] = []
    capped: list[int] = []
    spill: dict[str, Any] = {}
    try:
        for attempt in range(1, runs + 1):
            tag = f"{queries.QUERY_TAG_PREFIX}:{run_id}:{keyword}:{attempt}"
            runtime, query_id, hit_cap = _run_once(cur, objects, tag=tag, echo=echo, clock=clock, sleep=sleep)
            runtimes.append(runtime)
            warmth = "cold" if attempt == 1 else "warm"
            if hit_cap:
                capped.append(attempt)
                echo(f"  run {attempt} ({warmth}): stopped by the cost cap after {runtime:.0f}s  [{query_id}]")
            else:
                echo(f"  run {attempt} ({warmth}): {runtime:6.1f}s  [{query_id}]")
            if attempt == 1:
                spill = _read_spill(cur, query_id, sleep=sleep, echo=echo)
            if hit_cap:
                break  # later runs would hit the cap too; save the budget
    finally:
        # SUSPEND stops the billing and clears the local cache, so the next size starts cold.
        dedicated.suspend_quietly(cur, objects.warehouse, echo)
        up_s = clock() - resumed

    return SizeResult(
        keyword=keyword,
        label=label,
        credits_per_hour=warehouses.credits_per_hour(keyword, generation),
        runtimes_s=tuple(runtimes),
        gb_spill_local=_gb(spill.get("bytes_local")),
        gb_spill_remote=_gb(spill.get("bytes_remote")),
        capped_runs=tuple(capped),
        billed_s=up_s,
    )


def _run_once(
    cur: Any,
    objects: infra.BenchmarkObjects,
    *,
    tag: str,
    echo: Echo,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[float, str, bool]:
    """Run the query once under ``tag``; return (runtime_s, query_id, hit_cap)."""
    cur.execute(f"ALTER SESSION SET QUERY_TAG = '{tag}'")
    started = clock()
    hit_cap = False
    try:
        cur.execute(queries.BENCHMARK_QUERY)
        cur.fetchall()  # force full execution
    except Exception as exc:
        if not is_statement_timeout(exc):
            raise
        hit_cap = True
    client_s = clock() - started
    query_id = cur.sfqid or ""
    # Untag the session so the lookups below aren't counted as benchmark queries.
    cur.execute("ALTER SESSION UNSET QUERY_TAG")

    row = _lookup(
        cur,
        queries.ELAPSED_SQL.format(database=objects.database),
        query_id,
        ready="elapsed_ms",
        sleep=sleep,
        on_error=lambda exc: echo(f"  (couldn't read Snowflake's elapsed time: {exc}; using the client's)"),
    )
    runtime_s = client_s if row is None else float(row["elapsed_ms"]) / 1000
    return round(runtime_s, 1), query_id, hit_cap


def _read_spill(cur: Any, query_id: str, *, sleep: Callable[[float], None], echo: Echo) -> dict[str, Any]:
    """The cold run's spill from GET_QUERY_OPERATOR_STATS; empty if it can't be read."""
    stats = _lookup(
        cur,
        queries.SPILL_SQL,
        query_id,
        ready="operators",
        sleep=sleep,
        on_error=lambda exc: echo(f"  (couldn't read spill: {exc})"),
    )
    if stats is None:
        echo("  (spill isn't available yet; `warehouse-sizing report` will have it later)")
        return {}
    echo(f"  spill: {_gb(stats['bytes_local'])} GB local, {_gb(stats['bytes_remote'])} GB remote")
    return stats


def _lookup(
    cur: Any,
    sql: str,
    query_id: str,
    *,
    ready: str,
    sleep: Callable[[float], None],
    on_error: Callable[[Exception], None],
) -> dict[str, Any] | None:
    """One stats row for ``query_id``, once its ``ready`` column is set, or ``None``.

    Never raises: by now the query has been paid for, so a failed lookup should
    cost the results a number, not the whole sweep.
    """
    try:
        for attempt in range(1, LOOKUP_TRIES + 1):
            cur.execute(sql, (query_id,))
            found = rows(cur)
            if found and found[0].get(ready):
                return found[0]
            if attempt < LOOKUP_TRIES:
                sleep(LOOKUP_DELAY_SECONDS)
    except Exception as exc:  # best effort by design
        on_error(exc)
    return None


def _gb(value: Any) -> float | None:
    return None if value is None else round(float(value) / 1024**3, 2)
