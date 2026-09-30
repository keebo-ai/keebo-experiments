"""Run the same workload on an undersized and a right-sized warehouse.

``run_comparison`` takes an already-open connection — the injection seam — so it
runs against a connection opened by the CLI, a notebook, or a test fake. It
needs the objects ``infra.setup`` creates. No ``click`` here; problems raise
plain ``ValueError`` and the CLI turns them into clean messages.

Results are read back *live* from ``INFORMATION_SCHEMA`` (seconds of lag), so the
comparison can be shown the moment the run finishes. ``report.read_report``
reconciles against ``ACCOUNT_USAGE`` later for the exact billed credits.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from common import warehouses
from experiments.spillage.core import infra, queries

Echo = Callable[[str], None]

# The two sides of the comparison, in the order they run.
SIDES = ("undersized", "right_sized")

# Snowflake error 000630: the statement hit its STATEMENT_TIMEOUT_IN_SECONDS.
_TIMEOUT_ERRNO = 630


def _silent(_message: str) -> None:
    """The default progress sink; the CLI passes ``click.echo`` instead."""


def side_label(side: str) -> str:
    """``right_sized`` -> ``right-sized``, for display."""
    return side.replace("_", "-")


@dataclass(frozen=True)
class SideResult:
    """How the workload did on one side of the comparison."""

    side: str  # "undersized" or "right_sized"
    size_label: str
    credits_per_hour: float
    runtime_s: float  # Snowflake's elapsed time when available, else the client's
    gb_spill_local: float | None
    gb_spill_remote: float | None
    est_credits: float  # runtime x credits/hr, before the 60s minimum
    timed_out: bool = False  # hit the cost cap: runtime, spill, and cost are lower bounds


def run_comparison(
    conn: Any,
    *,
    objects: infra.DemoObjects,
    scenario: queries.Scenario,
    max_credits: float = queries.DEFAULT_MAX_CREDITS,
    run_id: str | None = None,
    echo: Echo = _silent,
    clock: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], None] = time.sleep,
) -> list[SideResult]:
    """Run the scenario's workload on the undersized, then the right-sized warehouse.

    ``max_credits`` caps the compute: each side gets half, enforced as a
    warehouse statement timeout. A side that hits it is cancelled and reported
    as timed out rather than failing the comparison.
    """
    run_id = run_id or datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    if not re.fullmatch(r"[0-9A-Za-z-]+", run_id):
        raise ValueError(f"run id must be letters, digits, and dashes, got {run_id!r}")
    sizes = {"undersized": scenario.undersized, "right_sized": scenario.right_sized}

    cur = conn.cursor()
    results: list[SideResult] = []
    try:
        state = infra.require(cur, objects, echo=echo)
        generation = state.generation
        workload = queries.build_workload(state.source_table, scenario.fanout)
        timeouts = {
            side: queries.statement_timeout_s(size, generation=generation, max_credits=max_credits)
            for side, size in sizes.items()
        }
        echo(f"Scenario: {scenario.label} — {scenario.blurb}")
        echo(f"Run id: {run_id} (how `spillage report` labels this run)")
        rows = scenario.fanout * queries.SOURCE_ROWS
        echo(f"Data: {state.source_table}, {scenario.fanout} copies of every row ({rows:,} groups).")
        caps = ", ".join(f"{warehouses.SIZE_LABEL[sizes[s]]} stops after {timeouts[s] / 60:.0f} min" for s in SIDES)
        echo(f"Cost cap: at most {max_credits:g} credits of Gen{generation} compute ({caps}).")

        cur.execute(f"USE WAREHOUSE {objects.warehouse}")
        # Turn off the result cache, else the second side returns for free.
        cur.execute("ALTER SESSION SET USE_CACHED_RESULT = FALSE")
        try:
            for side in SIDES:
                results.append(
                    _run_side(
                        cur,
                        objects,
                        side=side,
                        size=sizes[side],
                        generation=generation,
                        workload=workload,
                        query_tag=f"{queries.QUERY_TAG_PREFIX}:{run_id}:{scenario.name}:{side}",
                        timeout_s=timeouts[side],
                        echo=echo,
                        clock=clock,
                        sleep=sleep,
                    )
                )
        finally:
            _reset_for_idle(cur, objects.warehouse)

        for hint in calibration_hints(scenario, results):
            echo(f"\nNote: {hint}")
        echo("\nFor the exact BILLED credits (ACCOUNT_USAGE lags a few minutes), later run:  spillage report")
    finally:
        cur.close()
    return results


def _run_side(
    cur: Any,
    objects: infra.DemoObjects,
    *,
    side: str,
    size: str,
    generation: str,
    workload: str,
    query_tag: str,
    timeout_s: int,
    echo: Echo,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> SideResult:
    """Run the workload once, cold, on ``size`` and read back how it went."""
    label = warehouses.SIZE_LABEL[size]
    echo(f"\n=== {side_label(side)}: {label} ===")
    cur.execute(
        f"ALTER WAREHOUSE {objects.warehouse} SET WAREHOUSE_SIZE = {size} STATEMENT_TIMEOUT_IN_SECONDS = {timeout_s}"
    )
    cur.execute(f"ALTER WAREHOUSE {objects.warehouse} RESUME IF SUSPENDED")
    try:
        cur.execute(f"ALTER SESSION SET QUERY_TAG = '{query_tag}'")
        started = clock()
        timed_out = False
        try:
            cur.execute(workload)
            cur.fetchall()  # force full execution
        except Exception as exc:
            if getattr(exc, "errno", None) != _TIMEOUT_ERRNO:
                raise
            timed_out = True
        client_s = clock() - started
        query_id = getattr(cur, "sfqid", "") or ""
        # Untag the session so the stats lookup below isn't counted as workload.
        cur.execute("ALTER SESSION UNSET QUERY_TAG")

        if timed_out:
            echo(f"  stopped by the cost cap after {client_s:.0f}s  [{query_id}]")
        else:
            echo(f"  finished in {client_s:.1f}s  [{query_id}]")
        stats = _read_live_stats(cur, objects.database, query_id, sleep=sleep, echo=echo) or {}
    finally:
        # SUSPEND stops the billing and clears the local cache, so the next side starts cold.
        infra.suspend_quietly(cur, objects.warehouse, echo)

    elapsed_ms = stats.get("elapsed_ms")
    runtime_s = client_s if elapsed_ms is None else float(elapsed_ms) / 1000
    rate = warehouses.credits_per_hour(size, generation)
    result = SideResult(
        side=side,
        size_label=label,
        credits_per_hour=rate,
        runtime_s=round(runtime_s, 1),
        gb_spill_local=_gb(stats.get("bytes_local")),
        gb_spill_remote=_gb(stats.get("bytes_remote")),
        est_credits=round(runtime_s * rate / 3600, 5),
        timed_out=timed_out,
    )
    if result.gb_spill_local is not None:
        echo(f"  spill: {result.gb_spill_local} GB local, {result.gb_spill_remote} GB remote")
    return result


def _read_live_stats(
    cur: Any,
    database: str,
    query_id: str,
    *,
    sleep: Callable[[float], None],
    echo: Echo,
    tries: int = 5,
    delay: float = 2.0,
) -> dict[str, Any] | None:
    """One query's spill and elapsed time (see ``queries.LIVE_STATS_SQL``), or ``None``.

    Never raises: by now the query has been paid for, so a failed lookup should
    cost the comparison its spill numbers, not the whole run.
    """
    sql = queries.LIVE_STATS_SQL.format(database=database)
    try:
        for attempt in range(1, tries + 1):
            cur.execute(sql, (query_id, query_id))
            rows = cur.fetchall()
            columns = [col[0].lower() for col in cur.description]
            stats = dict(zip(columns, rows[0], strict=False)) if rows else {}
            if stats.get("elapsed_ms") is not None:
                return stats
            if attempt < tries:
                sleep(delay)
    except Exception as exc:  # best effort by design
        echo(f"  (couldn't read live stats: {exc})")
        return None
    echo("  (live stats not available yet — `spillage report` reads them from ACCOUNT_USAGE later)")
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


def calibration_hints(scenario: queries.Scenario, results: list[SideResult]) -> list[str]:
    """Say which way to turn ``--fanout`` when a run missed the scenario's target.

    Target: ``local`` wants the undersized warehouse to spill and the right-sized
    one not to; ``remote`` wants the undersized one to reach remote storage and
    the right-sized one to stay off it. A side that hit the cost cap gets a cap
    hint instead of a spill hint (its spill is only a lower bound).
    """
    by_side = {r.side: r for r in results}
    undersized, right_sized = by_side.get("undersized"), by_side.get("right_sized")
    if undersized is None or right_sized is None:
        return []
    fanout, lower, higher = scenario.fanout, max(1, scenario.fanout // 2), scenario.fanout * 2
    hints: list[str] = []
    if undersized.timed_out:
        hints.append(
            f"the {undersized.size_label} hit the cost cap before finishing, so its runtime and spill are "
            f"lower bounds. For a complete run, lower --fanout (e.g. {lower}) or raise --max-credits."
        )
    if right_sized.timed_out:
        hints.append(
            f"the {right_sized.size_label} hit the cost cap too, so there's no clean contrast. "
            f"Lower --fanout (e.g. {lower})."
        )
    if hints or undersized.gb_spill_local is None or right_sized.gb_spill_local is None:
        return hints

    if scenario.name == "remote":
        target = "remote spill"
        undersized_hit = bool(undersized.gb_spill_remote)
        right_sized_clean = not right_sized.gb_spill_remote
    else:
        target = "spill"
        undersized_hit = bool(undersized.gb_spill_local or undersized.gb_spill_remote)
        right_sized_clean = not (right_sized.gb_spill_local or right_sized.gb_spill_remote)
    if not undersized_hit:
        hints.append(
            f"the {undersized.size_label} didn't reach {target} at --fanout {fanout}. "
            f"Rerun with a higher --fanout (e.g. {higher})."
        )
    if not right_sized_clean:
        hints.append(
            f"the {right_sized.size_label} also hit {target} at --fanout {fanout}, so the contrast is weaker. "
            f"Rerun with a lower --fanout (e.g. {lower}) or a bigger --right-sized."
        )
    return hints
