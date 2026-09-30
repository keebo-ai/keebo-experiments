"""Run the same workload on an undersized and a right-sized warehouse, and tear down.

Both public functions take an already-open connection as their first argument —
the injection seam — so they run against a connection opened by the CLI, a
notebook, or a test fake. No ``click`` here; misuse raises plain ``ValueError``
and the CLI turns that into a clean error message.

Results are read back *live* from ``INFORMATION_SCHEMA`` (seconds of lag), so the
comparison can be shown the moment the run finishes. ``report.read_report``
reconciles against ``ACCOUNT_USAGE`` later for the exact billed credits.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from experiments.spillage.core import queries

# A no-op progress sink; the CLI passes ``click.echo`` instead.
Echo = Callable[[str], None]

ARMS = ("undersized", "right_sized")


def _silent(_message: str) -> None:
    pass


@dataclass(frozen=True)
class ArmResult:
    """One side of the comparison: the workload's cold run on one warehouse size."""

    arm: str
    size_keyword: str
    size_label: str
    credits_per_hour: int
    runtime_s: float  # client wall-clock
    elapsed_s: float | None  # Snowflake's total_elapsed_time, if available yet
    gb_spill_local: float | None
    gb_spill_remote: float | None
    partitions_scanned: int | None
    partitions_total: int | None
    est_credits: float  # active seconds x credits/hr, before the 60s minimum
    query_id: str
    timed_out: bool = False  # hit the cost cap; runtime and spill are lower bounds


def _gb(value: Any) -> float | None:
    return None if value is None else round(float(value) / 1024**3, 2)


# Snowflake error 000630: the statement hit its STATEMENT_TIMEOUT_IN_SECONDS.
_TIMEOUT_ERRNO = 630


def _is_timeout(exc: Exception) -> bool:
    return getattr(exc, "errno", None) == _TIMEOUT_ERRNO


def _check_sample_data(cur: Any, table: str) -> None:
    """Fail early with the mount command if a sample-data table is missing."""
    parts = table.split(".")
    if len(parts) != 3 or parts[0].upper() != "SNOWFLAKE_SAMPLE_DATA":
        return
    database, schema, name = parts
    cur.execute(f"SHOW TERSE OBJECTS LIKE '{name}' IN SCHEMA {database}.{schema}")
    if not cur.fetchall():
        raise ValueError(
            f"{table} not found. An ACCOUNTADMIN can mount the sample data:\n"
            "  CREATE DATABASE IF NOT EXISTS SNOWFLAKE_SAMPLE_DATA "
            "FROM SHARE SFC_SAMPLES.SAMPLE_DATA;\n"
            "  GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE_SAMPLE_DATA "
            "TO ROLE PUBLIC;"
        )


def _read_live_stats(
    cur: Any,
    query_id: str,
    *,
    sleep: Callable[[float], None],
    echo: Echo,
    tries: int = 5,
    delay: float = 2.0,
) -> dict[str, Any] | None:
    """Fetch one query's spill/partition stats from INFORMATION_SCHEMA, retrying briefly."""
    for attempt in range(1, tries + 1):
        cur.execute(queries.LIVE_STATS_SQL, (query_id,))
        rows = cur.fetchall()
        if rows:
            columns = [col[0].lower() for col in cur.description]
            return dict(zip(columns, rows[0], strict=False))
        if attempt < tries:
            sleep(delay)
    echo("  (live stats not available yet — `spillage report` will read them from ACCOUNT_USAGE)")
    return None


def run_comparison(
    conn: Any,
    *,
    scenario: queries.Scenario,
    warehouse_name: str = queries.DEFAULT_WAREHOUSE,
    runs: int = 1,
    max_credits: float = queries.DEFAULT_MAX_CREDITS,
    echo: Echo = _silent,
    clock: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], None] = time.sleep,
) -> list[ArmResult]:
    """Run the scenario's workload on the undersized, then the right-sized warehouse.

    Returns one :class:`ArmResult` per arm, built from each arm's cold (first)
    run. ``echo`` receives human-readable progress as the run goes.

    ``max_credits`` caps the run's compute: each arm gets half, enforced as a
    warehouse statement timeout. A run that hits it is cancelled and reported
    as timed out rather than failing the whole comparison.
    """
    queries.validate_identifier(scenario.table, "table")
    queries.validate_identifier(warehouse_name, "warehouse")
    sizes = {"undersized": scenario.undersized, "right_sized": scenario.right_sized}
    for arm, size in sizes.items():
        if size not in queries.CREDITS_PER_HOUR:
            raise ValueError(f"{arm} size must be one of {', '.join(queries.SIZE_KEYWORDS)}, got {size!r}")
    workload = queries.build_workload(scenario.fanout)
    timeouts = {
        arm: queries.statement_timeout_s(size, max_credits=max_credits, runs=runs) for arm, size in sizes.items()
    }

    cur = conn.cursor()
    results: list[ArmResult] = []
    try:
        _check_sample_data(cur, scenario.table)
        cur.execute(f"SET spill_table = '{scenario.table}'")

        echo(f"Scenario: {scenario.label} — {scenario.blurb}")
        caps = ", ".join(f"{queries.SIZE_LABEL[sizes[arm]]} stops after {timeouts[arm] / 60:.0f} min" for arm in ARMS)
        echo(f"Cost cap: at most {max_credits:g} credits of compute ({caps}).")
        echo(f"Creating warehouse {warehouse_name} ...")
        cur.execute(
            f"CREATE WAREHOUSE IF NOT EXISTS {warehouse_name} "
            f"WAREHOUSE_SIZE = {scenario.undersized} AUTO_SUSPEND = 60 AUTO_RESUME = TRUE "
            "INITIALLY_SUSPENDED = TRUE "
            "COMMENT = 'Keebo spillage demo - safe to drop'"
        )
        cur.execute(f"USE WAREHOUSE {warehouse_name}")
        # Turn off the result cache, else the second arm returns for free and
        # defeats the measurement.
        cur.execute("ALTER SESSION SET USE_CACHED_RESULT = FALSE")

        for arm in ARMS:
            size = sizes[arm]
            label = queries.SIZE_LABEL[size]
            echo(f"\n=== {arm.replace('_', '-')}: {label} ({size}) ===")
            cur.execute(f"ALTER WAREHOUSE {warehouse_name} SET WAREHOUSE_SIZE = {size}")
            cur.execute(f"ALTER WAREHOUSE {warehouse_name} SET STATEMENT_TIMEOUT_IN_SECONDS = {timeouts[arm]}")
            cur.execute(f"ALTER WAREHOUSE {warehouse_name} RESUME IF SUSPENDED")
            try:
                results.append(
                    _run_arm(
                        cur,
                        arm=arm,
                        size=size,
                        scenario=scenario,
                        workload=workload,
                        runs=runs,
                        timeout_s=timeouts[arm],
                        echo=echo,
                        clock=clock,
                        sleep=sleep,
                    )
                )
            finally:
                # Always stop billing, even if the arm failed. SUSPEND also clears
                # the local cache so the next arm starts cold.
                cur.execute(f"ALTER WAREHOUSE {warehouse_name} SUSPEND")

        for hint in calibration_hints(scenario, results):
            echo(f"\nNote: {hint}")
        echo(
            "\nFor exact BILLED credits (ACCOUNT_USAGE lags a few minutes), later run:  "
            "keebo-experiments spillage report"
        )
    finally:
        cur.close()
    return results


def _run_arm(
    cur: Any,
    *,
    arm: str,
    size: str,
    scenario: queries.Scenario,
    workload: str,
    runs: int,
    timeout_s: int,
    echo: Echo,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> ArmResult:
    """Run the workload ``runs`` times on the current size and build the arm's result."""
    cold_runtime, cold_qid, timed_out = 0.0, "", False
    for attempt in range(1, runs + 1):
        cur.execute(f"ALTER SESSION SET QUERY_TAG = '{queries.QUERY_TAG_PREFIX}:{scenario.name}:{arm}:{attempt}'")
        started = clock()
        hit_cap = False
        try:
            cur.execute(workload)
            cur.fetchall()  # force full execution
        except Exception as exc:
            if not _is_timeout(exc):
                raise
            hit_cap = True
        elapsed = clock() - started
        query_id = getattr(cur, "sfqid", "") or ""
        warmth = "cold" if attempt == 1 else "warm"
        if hit_cap:
            echo(f"  run {attempt} ({warmth}): stopped by the cost cap after {timeout_s}s  [{query_id}]")
        else:
            echo(f"  run {attempt} ({warmth}): {elapsed:7.1f}s  [{query_id}]")
        if attempt == 1:
            cold_runtime, cold_qid, timed_out = elapsed, query_id, hit_cap
        if hit_cap:
            break  # a repeat would hit the cap too; save the budget

    stats = _read_live_stats(cur, cold_qid, sleep=sleep, echo=echo) or {}
    elapsed_ms = stats.get("total_elapsed_time")
    elapsed_s = None if elapsed_ms is None else round(float(elapsed_ms) / 1000, 1)
    credits_per_hour = queries.CREDITS_PER_HOUR[size]
    active_s = elapsed_s if elapsed_s is not None else cold_runtime
    result = ArmResult(
        arm=arm,
        size_keyword=size,
        size_label=queries.SIZE_LABEL[size],
        credits_per_hour=credits_per_hour,
        runtime_s=round(cold_runtime, 1),
        elapsed_s=elapsed_s,
        gb_spill_local=_gb(stats.get("bytes_local")),
        gb_spill_remote=_gb(stats.get("bytes_remote")),
        partitions_scanned=stats.get("partitions_scanned"),
        partitions_total=stats.get("partitions_total"),
        est_credits=round(active_s * credits_per_hour / 3600, 5),
        query_id=cold_qid,
        timed_out=timed_out,
    )
    if result.gb_spill_local is not None:
        echo(f"  spill: {result.gb_spill_local} GB local, {result.gb_spill_remote} GB remote")
    return result


def calibration_hints(scenario: queries.Scenario, results: list[ArmResult]) -> list[str]:
    """Say which way to turn ``--fanout`` when a run missed the scenario's target.

    Target: ``local`` wants the undersized warehouse to spill and the right-sized
    one not to; ``remote`` wants the undersized one to reach remote storage and
    the right-sized one to stay off it. A size that hit the cost cap gets a
    cap hint instead of a spill hint (its spill is only a lower bound).
    """
    by_arm = {r.arm: r for r in results}
    under, right = by_arm.get("undersized"), by_arm.get("right_sized")
    if under is None or right is None:
        return []
    fanout, lower, higher = scenario.fanout, max(1, scenario.fanout // 2), scenario.fanout * 2
    hints: list[str] = []
    if under.timed_out:
        hints.append(
            f"the {under.size_label} hit the cost cap before finishing, so its runtime and spill are lower "
            f"bounds. For a complete run, lower --fanout (e.g. {lower}) or raise --max-credits."
        )
    if right.timed_out:
        hints.append(
            f"the {right.size_label} hit the cost cap too, so there's no clean contrast. Lower --fanout (e.g. {lower})."
        )
    if hints or under.gb_spill_local is None or right.gb_spill_local is None:
        return hints

    if scenario.name == "remote":
        target = "remote spill"
        under_hit = bool(under.gb_spill_remote)
        right_clean = not right.gb_spill_remote
    else:
        target = "spill"
        under_hit = bool(under.gb_spill_local or under.gb_spill_remote)
        right_clean = not (right.gb_spill_local or right.gb_spill_remote)
    if not under_hit:
        hints.append(
            f"the {under.size_label} didn't reach {target} at --fanout {fanout}. "
            f"Rerun with a higher --fanout (e.g. {higher})."
        )
    if not right_clean:
        hints.append(
            f"the {right.size_label} also hit {target} at --fanout {fanout}, so the contrast is weaker. "
            f"Rerun with a lower --fanout (e.g. {lower}) or a bigger --right-sized."
        )
    return hints


def drop_warehouse(
    conn: Any,
    *,
    warehouse_name: str = queries.DEFAULT_WAREHOUSE,
    echo: Echo = _silent,
) -> None:
    """Drop the demo warehouse and nothing else."""
    queries.validate_identifier(warehouse_name, "warehouse")
    cur = conn.cursor()
    try:
        cur.execute(f"DROP WAREHOUSE IF EXISTS {warehouse_name}")
        echo(f"Dropped {warehouse_name}.")
    finally:
        cur.close()
