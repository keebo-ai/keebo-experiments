"""Turn a sweep into tables: live results now, ACCOUNT_USAGE reconciliation later (Steps 10-16).

:func:`live_tables` and :func:`summary_lines` format what ``sweep.sweep_sizes``
returned, the moment it finishes. :func:`read_report` reads timings, spill, and
billed credits back from ``ACCOUNT_USAGE`` for one run. Both return
:class:`~common.tables.ReportTable` values; the CLI prints them. No ``click`` here.
"""

from __future__ import annotations

import re
from typing import Any

from common import warehouses
from common.tables import ReportTable
from experiments.warehouse_sizing_benchmark.core import infra, queries
from experiments.warehouse_sizing_benchmark.core.sweep import SizeResult

_MISSING = "—"


def _num(value: float | None, digits: int) -> str:
    return _MISSING if value is None else f"{value:.{digits}f}"


def _measured(result: SizeResult, value: float | None, digits: int) -> str:
    """Format a measurement, marking it a lower bound (``+``) if that size hit the cost cap."""
    text = _num(value, digits)
    return f"{text}+" if result.timed_out and value is not None else text


# --------------------------------------------------------------------------- #
# Live results
# --------------------------------------------------------------------------- #
def live_tables(results: list[SizeResult]) -> list[ReportTable]:
    """The per-size table for a sweep, plus a side-by-side when exactly two sizes ran."""
    tables = [
        ReportTable(
            None,
            "Results by size (spill is from the cold run)",
            [
                "size",
                "credits_per_hr",
                "runs",
                "cold_s",
                "warm_s",
                "spill_local_gb",
                "spill_remote_gb",
                "query_credits",
                "billed_credits",
            ],
            [
                (
                    r.label,
                    f"{r.credits_per_hour:g}",
                    len(r.runtimes_s),
                    _measured(r, r.cold_s, 1),
                    _measured(r, r.warm_s, 1),
                    _measured(r, r.gb_spill_local, 2),
                    _measured(r, r.gb_spill_remote, 2),
                    _measured(r, r.query_credits, 5),
                    _measured(r, r.billed_credits, 5),
                )
                for r in results
            ],
        )
    ]
    if len(results) == 2:
        smaller, larger = sorted(results, key=lambda r: r.credits_per_hour)
        tables.append(
            ReportTable(
                None,
                f"{smaller.label} vs {larger.label}",
                ["metric", smaller.label, larger.label, "change"],
                _side_by_side_rows(smaller, larger),
            )
        )
    return tables


def _side_by_side_rows(smaller: SizeResult, larger: SizeResult) -> list[tuple[str, str, str, str]]:
    """One row per metric: both measurements, then what changed going to the larger size."""
    # A capped larger size has only lower bounds for spill, so no spill change can be claimed.
    spill_known = not larger.timed_out
    changes = {
        "cold_s": _speedup(smaller, larger),
        "gb_spill_local": _spill_change(smaller.gb_spill_local, larger.gb_spill_local) if spill_known else _MISSING,
        "gb_spill_remote": _spill_change(smaller.gb_spill_remote, larger.gb_spill_remote) if spill_known else _MISSING,
        "query_credits": _cost_change(smaller, larger, "query_credits"),
        "billed_credits": _cost_change(smaller, larger, "billed_credits"),
    }
    metrics = [
        ("runtime, cold (s)", "cold_s", 1),
        ("local spill (GB)", "gb_spill_local", 2),
        ("remote spill (GB)", "gb_spill_remote", 2),
        ("credits per query", "query_credits", 5),
        ("credits billed for this run", "billed_credits", 5),
    ]
    return [
        (
            label,
            _measured(smaller, getattr(smaller, field), digits),
            _measured(larger, getattr(larger, field), digits),
            changes[field],
        )
        for label, field, digits in metrics
    ]


def _speedup(smaller: SizeResult, larger: SizeResult) -> str:
    if larger.timed_out or not smaller.cold_s or not larger.cold_s:
        return _MISSING
    ratio = smaller.cold_s / larger.cold_s
    text = f"{ratio:.1f}x faster" if ratio >= 1 else f"{1 / ratio:.1f}x slower"
    return f"at least {text}" if smaller.timed_out else text


def _spill_change(smaller: float | None, larger: float | None) -> str:
    if smaller is None or larger is None or smaller <= 0:
        return _MISSING
    if larger == 0:
        return "eliminated"
    return f"{(smaller - larger) / smaller * 100:.0f}% less"


def _cost_change(smaller: SizeResult, larger: SizeResult, field: str) -> str:
    before, after = getattr(smaller, field), getattr(larger, field)
    if smaller.timed_out or larger.timed_out or not before or not after:
        return _MISSING
    diff = (after - before) / before * 100
    if round(diff) == 0:
        return "about the same"
    return f"{abs(diff):.0f}% cheaper" if diff < 0 else f"{diff:.0f}% pricier"


def summary_lines(results: list[SizeResult]) -> list[str]:
    """The sweep's wrap-up: credits used, the cheapest size per query, and calibration hints."""
    if not results:
        return []
    total = sum(r.billed_credits for r in results)
    amount = "at least" if any(r.timed_out for r in results) else "about"
    lines = [
        f"This run used {amount} {total:.3f} credits. Each size bills at least 60 seconds when it resumes. "
        "For the exact bill, run `warehouse-sizing report` in an hour or two."
    ]
    finished = [r for r in results if not r.timed_out]
    if len(finished) > 1:
        cheapest = min(finished, key=lambda r: r.query_credits)
        lines.append(f"Cheapest per query: {cheapest.label} ({cheapest.query_credits:.5f} credits).")
    if len(results) == 2:
        lines.extend(_calibration_hints(*sorted(results, key=lambda r: r.credits_per_hour)))
    return lines


def _calibration_hints(smaller: SizeResult, larger: SizeResult) -> list[str]:
    """For a two-size comparison: say how to sharpen it when it missed."""
    hints = []
    if smaller.timed_out:
        hints.append(
            f"The {smaller.label} hit the cost cap, so its numbers are a floor. Raise --max-credits to let it finish."
        )
    if smaller.gb_spill_local is not None and not smaller.timed_out and not smaller.gb_spill_local:
        hints.append(
            f"The {smaller.label} didn't spill, so there's nothing to compare. Try a smaller first --size, or a "
            "bigger --table like SNOWFLAKE_SAMPLE_DATA.TPCH_SF1000.LINEITEM if you can read the sample data."
        )
    # A little residual spill on the larger size still makes the point; flag it only when it's a real share.
    if larger.gb_spill_local and not larger.timed_out and larger.gb_spill_local >= 0.25 * (smaller.gb_spill_local or 0):
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
    up yet — wait and rerun.
    """
    hours = int(hours)
    if run_id is not None and not re.fullmatch(r"[0-9A-Za-z-]+", run_id):
        raise ValueError(f"run id must be letters, digits, and dashes, got {run_id!r}")

    cur = conn.cursor()
    tables: list[ReportTable] = []
    try:
        # Only the objects: the report reads ACCOUNT_USAGE, not the source table.
        generation = infra.require_objects(cur, objects)
        multiplier = warehouses.GEN2_MULTIPLIER if generation == "2" else 1
        cur.execute(f"USE WAREHOUSE {objects.warehouse}")
        if run_id is None:
            cur.execute(queries.LATEST_RUN_SQL.format(wh=objects.warehouse, hours=hours))
            rows = cur.fetchall()
            run_id = rows[0][0] if rows else None
        if run_id is None:
            return None, []
        tag = f"{queries.QUERY_TAG_PREFIX}:{run_id}:"
        for step, title, sql in queries.REPORT_STEPS:
            cur.execute(sql.format(hours=hours, wh=objects.warehouse, tag=tag, multiplier=multiplier))
            columns = [col[0] for col in cur.description]
            tables.append(ReportTable(step, title, columns, list(cur.fetchall())))
    finally:
        cur.close()
    return run_id, tables
