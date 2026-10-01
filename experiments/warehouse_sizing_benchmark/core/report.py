"""Turn a sweep into tables: results now, ACCOUNT_USAGE reconciliation later (Steps 10-16).

:func:`live_tables` and :func:`summary_lines` format what ``sweep.sweep_sizes``
returned, the moment it finishes. :func:`read_report` reads timings, spill, and
billed credits back from ``ACCOUNT_USAGE`` for one run. They return
:class:`~common.tables.ReportTable` values and lines; the CLI prints them. No
``click`` here.
"""

from __future__ import annotations

from typing import Any

from common import dedicated, warehouses
from common.tables import ReportTable
from experiments.warehouse_sizing_benchmark.core import infra, queries
from experiments.warehouse_sizing_benchmark.core.sweep import SizeResult

_MISSING = "—"

# Flag the larger size's spill only when it's a real share of the smaller one's.
RESIDUAL_SPILL_SHARE = 0.25


def _cell(value: float | None, digits: int, *, capped: bool = False) -> str:
    """Format a measurement; ``+`` marks a lower bound from a run the cost cap stopped."""
    if value is None:
        return _MISSING
    return f"{value:.{digits}f}{'+' if capped else ''}"


# --------------------------------------------------------------------------- #
# Live results
# --------------------------------------------------------------------------- #
def live_tables(results: list[SizeResult]) -> list[ReportTable]:
    """The per-size table for a sweep, plus a side-by-side when exactly two sizes ran."""
    with_warm = any(len(r.runtimes_s) > 1 for r in results)
    columns = ["size", "credits/hr", "runs", "cold (s)", *(["warm (s)"] if with_warm else [])]
    columns += ["local spill (GB)", "remote spill (GB)", "credits/query", "credits billed"]
    table_rows = []
    for r in results:
        any_capped = bool(r.capped_runs)
        row = [r.label, f"{r.credits_per_hour:g}", len(r.runtimes_s), _cell(r.cold_s, 1, capped=r.cold_capped)]
        if with_warm:
            row.append(_cell(r.warm_s, 1, capped=r.warm_capped))
        row += [
            _cell(r.gb_spill_local, 2, capped=r.cold_capped),
            _cell(r.gb_spill_remote, 2, capped=r.cold_capped),
            _cell(r.query_credits, 5, capped=any_capped),
            _cell(r.billed_credits, 5, capped=any_capped),
        ]
        table_rows.append(tuple(row))
    tables = [
        ReportTable(step=None, title="Results by size (spill is from the cold run)", columns=columns, rows=table_rows)
    ]

    if len(results) == 2:
        smaller, larger = sorted(results, key=lambda r: r.credits_per_hour)
        tables.append(
            ReportTable(
                step=None,
                title=f"{smaller.label} vs {larger.label}",
                columns=["metric", smaller.label, larger.label, "change"],
                rows=_side_by_side_rows(smaller, larger),
            )
        )
    return tables


def _side_by_side_rows(smaller: SizeResult, larger: SizeResult) -> list[tuple[str, str, str, str]]:
    """One row per metric: both measurements, then what changed going to the larger size."""
    small_any, large_any = bool(smaller.capped_runs), bool(larger.capped_runs)
    return [
        (
            "runtime, cold (s)",
            _cell(smaller.cold_s, 1, capped=smaller.cold_capped),
            _cell(larger.cold_s, 1, capped=larger.cold_capped),
            _speedup(smaller, larger),
        ),
        (
            "local spill (GB)",
            _cell(smaller.gb_spill_local, 2, capped=smaller.cold_capped),
            _cell(larger.gb_spill_local, 2, capped=larger.cold_capped),
            _MISSING if larger.cold_capped else _spill_change(smaller.gb_spill_local, larger.gb_spill_local),
        ),
        (
            "remote spill (GB)",
            _cell(smaller.gb_spill_remote, 2, capped=smaller.cold_capped),
            _cell(larger.gb_spill_remote, 2, capped=larger.cold_capped),
            _MISSING if larger.cold_capped else _spill_change(smaller.gb_spill_remote, larger.gb_spill_remote),
        ),
        (
            "credits per query",
            _cell(smaller.query_credits, 5, capped=small_any),
            _cell(larger.query_credits, 5, capped=large_any),
            _MISSING if small_any or large_any else _cost_change(smaller.query_credits, larger.query_credits),
        ),
        (
            "credits billed for this run",
            _cell(smaller.billed_credits, 5, capped=small_any),
            _cell(larger.billed_credits, 5, capped=large_any),
            _MISSING if small_any or large_any else _cost_change(smaller.billed_credits, larger.billed_credits),
        ),
    ]


def _speedup(smaller: SizeResult, larger: SizeResult) -> str:
    if larger.cold_capped or not smaller.cold_s or not larger.cold_s:
        return _MISSING
    ratio = smaller.cold_s / larger.cold_s
    text = f"{ratio:.1f}x faster" if ratio >= 1 else f"{1 / ratio:.1f}x slower"
    return f"at least {text}" if smaller.cold_capped else text


def _spill_change(smaller: float | None, larger: float | None) -> str:
    if smaller is None or larger is None:
        return _MISSING
    if smaller == 0 and larger == 0:
        return "none"
    if smaller <= 0:
        return _MISSING
    if larger == 0:
        return "eliminated"
    return f"{(smaller - larger) / smaller * 100:.0f}% less"


def _cost_change(smaller: float, larger: float) -> str:
    if not smaller or not larger:
        return _MISSING
    ratio = larger / smaller
    if ratio >= 2:
        return f"{ratio:.1f}x as much"
    diff = (ratio - 1) * 100
    if round(diff) == 0:
        return "about the same"
    return f"{abs(diff):.0f}% cheaper" if diff < 0 else f"{diff:.0f}% more"


def summary_lines(results: list[SizeResult]) -> list[str]:
    """The sweep's wrap-up: credits used, the cheapest size per query, and hints for a comparison."""
    if not results:
        return []
    total = sum(r.billed_credits for r in results)
    amount = "at least" if any(r.capped_runs for r in results) else "about"
    lines = [
        f"This run used {amount} {total:.3f} credits, counting the 60-second minimum each size bills when it "
        "resumes. Step 15 of `warehouse-sizing report` shows what Snowflake billed; it can take up to 3 hours "
        "to show up."
    ]
    finished = [r for r in results if not r.capped_runs]
    if len(finished) > 1:
        cheapest = min(finished, key=lambda r: r.query_credits)
        lines.append(f"Cheapest per query: {cheapest.label} ({cheapest.query_credits:.5f} credits).")
    if len(results) == 2:
        smaller, larger = sorted(results, key=lambda r: r.credits_per_hour)
        lines.extend(_minimum_notes([smaller, larger]))
        lines.extend(_calibration_hints(smaller, larger))
    return lines


def _minimum_notes(results: list[SizeResult]) -> list[str]:
    """Say why a size that ran under a minute bills more than its runtime suggests."""
    notes = []
    for r in results:
        up_s = max(r.billed_s or 0, sum(r.runtimes_s))
        if not r.capped_runs and up_s < warehouses.BILLING_MINIMUM_SECONDS:
            notes.append(
                f"The {r.label} ran for {up_s:.0f}s but bills the 60-second minimum. On a warehouse that's "
                f"already running, each query costs {r.query_credits:.5f} credits."
            )
    return notes


def _calibration_hints(smaller: SizeResult, larger: SizeResult) -> list[str]:
    """For a two-size comparison: say how to sharpen it when it missed."""
    hints = []
    if smaller.capped_runs:
        hints.append(
            f"The {smaller.label} hit the cost cap, so its numbers are a floor. Raise --max-credits to let it finish."
        )
    if smaller.gb_spill_local == 0 and not smaller.cold_capped:
        if smaller.keyword == warehouses.SIZE_KEYWORDS[0]:
            fix = "Try a bigger --table, like SNOWFLAKE_SAMPLE_DATA.TPCH_SF1000.LINEITEM, and raise --max-credits."
        else:
            fix = (
                "Try a smaller first --size, or a bigger --table like SNOWFLAKE_SAMPLE_DATA.TPCH_SF1000.LINEITEM "
                "with a higher --max-credits."
            )
        hints.append(f"The {smaller.label} didn't spill, so there's nothing to compare. {fix}")
    if (
        smaller.gb_spill_local is not None
        and larger.gb_spill_local
        and not larger.cold_capped
        and larger.gb_spill_local >= RESIDUAL_SPILL_SHARE * smaller.gb_spill_local
    ):
        hints.append(
            f"The {larger.label} spilled too, so the difference is smaller than it could be. Try a bigger second "
            "--size or a smaller --table."
        )
    return hints


# --------------------------------------------------------------------------- #
# ACCOUNT_USAGE reconciliation (Steps 10-16)
# --------------------------------------------------------------------------- #
def read_report(
    conn: Any,
    *,
    objects: infra.BenchmarkObjects,
    hours: int = 24,
    run_id: str | None = None,
) -> tuple[str | None, list[ReportTable]]:
    """Run each reporting query for one run on the benchmark warehouse (Steps 10-16).

    Returns the run id reported (the latest, unless one is given) and one
    :class:`ReportTable` per step. Empty ``rows`` mean ACCOUNT_USAGE hasn't caught
    up yet; wait and rerun.
    """
    if int(hours) < 1:
        raise ValueError(f"hours must be at least 1, got {hours}")
    if run_id is not None:
        queries.validate_run_id(run_id)

    cur = conn.cursor()
    tables: list[ReportTable] = []
    try:
        # Only the warehouse: the report reads ACCOUNT_USAGE, not the database or the source table.
        generation = infra.require_warehouse(cur, objects)
        cur.execute(f"USE WAREHOUSE {objects.warehouse}")
        try:
            if run_id is None:
                cur.execute(queries.LATEST_RUN_SQL.format(wh=objects.warehouse, hours=int(hours)))
                found = cur.fetchall()
                run_id = found[0][0] if found else None
            if run_id is None:
                return None, []
            for step, title, sql in queries.REPORT_STEPS:
                cur.execute(
                    queries.report_sql(
                        sql, warehouse=objects.warehouse, run_id=run_id, hours=hours, generation=generation
                    )
                )
                columns = [col[0] for col in cur.description]
                tables.append(ReportTable(step, title, columns, list(cur.fetchall())))
        finally:
            # Don't wait for AUTO_SUSPEND: the report is done with the warehouse.
            dedicated.suspend_quietly(cur, objects.warehouse)
    finally:
        cur.close()
    return run_id, tables
