"""Turn a spillage run into report tables, and reconcile against ACCOUNT_USAGE.

:func:`comparison_tables` formats the live results of a run (the A/B table and
the verdict). :func:`read_report` reads the authoritative billed credits and
spill back from ``ACCOUNT_USAGE`` afterwards. Both return
:class:`~common.tables.ReportTable` values; the CLI prints them. No ``click`` here.
"""

from __future__ import annotations

from typing import Any

from common.tables import ReportTable
from experiments.spillage.core import queries
from experiments.spillage.core.run import ArmResult

_MISSING = "—"


def _num(value: float | None, digits: int) -> str:
    return _MISSING if value is None else f"{value:.{digits}f}"


def _bound(r: ArmResult, value: float | None, digits: int) -> str:
    """Format a measurement, marking it a lower bound (``+``) if the run hit the cost cap."""
    text = _num(value, digits)
    return f"{text}+" if r.timed_out and value is not None else text


def _speedup(u: ArmResult, g: ArmResult) -> str:
    if g.timed_out or not u.runtime_s or not g.runtime_s:
        return _MISSING
    ratio = u.runtime_s / g.runtime_s
    text = f"{ratio:.1f}x faster" if ratio >= 1 else f"{1 / ratio:.1f}x slower"
    return f"at least {text}" if u.timed_out else text


def _spill_change(undersized: float | None, right_sized: float | None) -> str:
    if undersized is None or right_sized is None or undersized <= 0:
        return _MISSING
    if right_sized == 0:
        return "eliminated"
    return f"{(undersized - right_sized) / undersized * 100:.0f}% less"


def _cost_change(undersized: float, right_sized: float) -> str:
    if not undersized or not right_sized:
        return _MISSING
    diff = (right_sized - undersized) / undersized * 100
    if round(diff) == 0:
        return "about the same"
    return f"{abs(diff):.0f}% cheaper" if diff < 0 else f"{diff:.0f}% pricier"


def comparison_tables(results: list[ArmResult], *, scenario: queries.Scenario) -> list[ReportTable]:
    """The live A/B table for a run, plus the verdict when both arms ran."""
    rows = [
        (
            r.arm.replace("_", "-"),
            r.size_label,
            r.credits_per_hour,
            _bound(r, r.runtime_s, 1),
            _bound(r, r.gb_spill_local, 2),
            _bound(r, r.gb_spill_remote, 2),
            _bound(r, r.est_credits, 5),
        )
        for r in results
    ]
    tables = [
        ReportTable(
            1,
            f"Same workload, two warehouse sizes — {scenario.label}",
            ["arm", "size", "credits_per_hr", "runtime_s", "spill_local_gb", "spill_remote_gb", "est_credits"],
            rows,
        )
    ]

    by_arm = {r.arm: r for r in results}
    if "undersized" in by_arm and "right_sized" in by_arm:
        u, g = by_arm["undersized"], by_arm["right_sized"]
        tables.append(
            ReportTable(
                2,
                "The verdict (right-sized vs undersized)",
                ["metric", "undersized", "right_sized", "change"],
                [
                    ("runtime (s)", _bound(u, u.runtime_s, 1), _bound(g, g.runtime_s, 1), _speedup(u, g)),
                    (
                        "local spill (GB)",
                        _bound(u, u.gb_spill_local, 2),
                        _bound(g, g.gb_spill_local, 2),
                        _MISSING if g.timed_out else _spill_change(u.gb_spill_local, g.gb_spill_local),
                    ),
                    (
                        "remote spill (GB)",
                        _bound(u, u.gb_spill_remote, 2),
                        _bound(g, g.gb_spill_remote, 2),
                        _MISSING if g.timed_out else _spill_change(u.gb_spill_remote, g.gb_spill_remote),
                    ),
                    (
                        "est. credits",
                        _bound(u, u.est_credits, 5),
                        _bound(g, g.est_credits, 5),
                        _MISSING if u.timed_out or g.timed_out else _cost_change(u.est_credits, g.est_credits),
                    ),
                ],
            )
        )
    return tables


def read_report(
    conn: Any,
    *,
    warehouse_name: str = queries.DEFAULT_WAREHOUSE,
    hours: int = 6,
) -> list[ReportTable]:
    """Run each reconciliation query and return one :class:`ReportTable` per step.

    Empty ``rows`` mean ACCOUNT_USAGE hasn't caught up yet — wait and rerun.
    """
    queries.validate_identifier(warehouse_name, "warehouse")
    hours = int(hours)

    cur = conn.cursor()
    tables: list[ReportTable] = []
    try:
        for step, title, sql in queries.REPORT_STEPS:
            cur.execute(sql.format(hours=hours, wh=warehouse_name))
            columns = [col[0] for col in cur.description]
            tables.append(ReportTable(step, title, columns, list(cur.fetchall())))
    finally:
        cur.close()
    return tables
