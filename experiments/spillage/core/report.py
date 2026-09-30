"""Turn a spillage run into report tables, and reconcile against ACCOUNT_USAGE.

:func:`comparison_tables` formats the live results of a run (the side-by-side
table and the verdict). :func:`read_report` reads the authoritative spill and
billed credits back from ``ACCOUNT_USAGE`` afterwards. Both return
:class:`~common.tables.ReportTable` values; the CLI prints them. No ``click`` here.
"""

from __future__ import annotations

from typing import Any

from common.tables import ReportTable
from experiments.spillage.core import infra, queries
from experiments.spillage.core.run import SideResult, side_label

_MISSING = "—"


def _num(value: float | None, digits: int) -> str:
    return _MISSING if value is None else f"{value:.{digits}f}"


def _measured(result: SideResult, value: float | None, digits: int) -> str:
    """Format a measurement, marking it a lower bound (``+``) if that side hit the cost cap."""
    text = _num(value, digits)
    return f"{text}+" if result.timed_out and value is not None else text


def _speedup(undersized: SideResult, right_sized: SideResult) -> str:
    if right_sized.timed_out or not undersized.runtime_s or not right_sized.runtime_s:
        return _MISSING
    ratio = undersized.runtime_s / right_sized.runtime_s
    text = f"{ratio:.1f}x faster" if ratio >= 1 else f"{1 / ratio:.1f}x slower"
    return f"at least {text}" if undersized.timed_out else text


def _spill_change(undersized: float | None, right_sized: float | None) -> str:
    if undersized is None or right_sized is None or undersized <= 0:
        return _MISSING
    if right_sized == 0:
        return "eliminated"
    return f"{(undersized - right_sized) / undersized * 100:.0f}% less"


def _cost_change(undersized: SideResult, right_sized: SideResult) -> str:
    if undersized.timed_out or right_sized.timed_out or not undersized.est_credits or not right_sized.est_credits:
        return _MISSING
    diff = (right_sized.est_credits - undersized.est_credits) / undersized.est_credits * 100
    if round(diff) == 0:
        return "about the same"
    return f"{abs(diff):.0f}% cheaper" if diff < 0 else f"{diff:.0f}% pricier"


def comparison_tables(results: list[SideResult], *, scenario: queries.Scenario) -> list[ReportTable]:
    """The live side-by-side table for a run, plus the verdict when both sides ran."""
    tables = [
        ReportTable(
            1,
            f"Same workload, two warehouse sizes — {scenario.label}",
            ["side", "size", "credits_per_hr", "runtime_s", "spill_local_gb", "spill_remote_gb", "est_credits"],
            [
                (
                    side_label(r.side),
                    r.size_label,
                    f"{r.credits_per_hour:g}",
                    _measured(r, r.runtime_s, 1),
                    _measured(r, r.gb_spill_local, 2),
                    _measured(r, r.gb_spill_remote, 2),
                    _measured(r, r.est_credits, 5),
                )
                for r in results
            ],
        )
    ]

    by_side = {r.side: r for r in results}
    if "undersized" in by_side and "right_sized" in by_side:
        tables.append(
            ReportTable(
                2,
                "The verdict (right-sized vs undersized)",
                ["metric", "undersized", "right-sized", "change"],
                _verdict_rows(by_side["undersized"], by_side["right_sized"]),
            )
        )
    return tables


def _verdict_rows(undersized: SideResult, right_sized: SideResult) -> list[tuple[str, str, str, str]]:
    """One row per metric: both measurements, then what changed."""
    # A capped right-sized run has only lower bounds for spill, so no spill change can be claimed.
    spill_known = not right_sized.timed_out
    changes = {
        "runtime_s": _speedup(undersized, right_sized),
        "gb_spill_local": _spill_change(undersized.gb_spill_local, right_sized.gb_spill_local)
        if spill_known
        else _MISSING,
        "gb_spill_remote": _spill_change(undersized.gb_spill_remote, right_sized.gb_spill_remote)
        if spill_known
        else _MISSING,
        "est_credits": _cost_change(undersized, right_sized),
    }
    metrics = [
        ("runtime (s)", "runtime_s", 1),
        ("local spill (GB)", "gb_spill_local", 2),
        ("remote spill (GB)", "gb_spill_remote", 2),
        ("est. credits", "est_credits", 5),
    ]
    return [
        (
            label,
            _measured(undersized, getattr(undersized, field), digits),
            _measured(right_sized, getattr(right_sized, field), digits),
            changes[field],
        )
        for label, field, digits in metrics
    ]


def read_report(conn: Any, *, objects: infra.DemoObjects, hours: int = 24) -> list[ReportTable]:
    """Run each reconciliation query on the demo warehouse; one :class:`ReportTable` per step.

    Empty ``rows`` mean ACCOUNT_USAGE hasn't caught up yet — wait and rerun.
    """
    hours = int(hours)
    cur = conn.cursor()
    tables: list[ReportTable] = []
    try:
        infra.require(cur, objects)
        cur.execute(f"USE WAREHOUSE {objects.warehouse}")
        for step, title, sql in queries.REPORT_STEPS:
            cur.execute(
                sql.format(
                    wh=objects.warehouse,
                    hours=hours,
                    tag=queries.QUERY_TAG_PREFIX,
                    workload=queries.WORKLOAD_PREFIX,
                )
            )
            columns = [col[0] for col in cur.description]
            tables.append(ReportTable(step, title, columns, list(cur.fetchall())))
    finally:
        cur.close()
    return tables
