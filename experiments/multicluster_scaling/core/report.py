"""Shape the scaling descriptions into a report table and notes for the CLI.

No ``click`` here — this builds shared :class:`~common.tables.ReportTable`
values; :mod:`common.render` prints them.
"""

from __future__ import annotations

from common.tables import ReportTable
from experiments.multicluster_scaling.core.analysis import Distribution, WarehouseScaling

_COLUMNS = [
    "warehouse",
    "peak clusters",
    "marginal spin-ups",
    "cluster life p50 (s)",
    "cluster life p90 (s)",
    "occupancy p50",
    "occupancy p90",
]


def _seconds(distribution: Distribution | None, attr: str) -> str:
    if distribution is None:
        return "-"
    return f"{getattr(distribution, attr):.0f}"


def _percent(distribution: Distribution | None, attr: str) -> str:
    if distribution is None:
        return "-"
    return f"{getattr(distribution, attr):.0%}"


def summary_table(scalings: list[WarehouseScaling], *, days: int) -> ReportTable:
    """One row per warehouse describing how its clusters scaled."""
    rows = [
        (
            scaling.warehouse,
            scaling.peak_clusters,
            scaling.marginal_spinups,
            _seconds(scaling.lifetime_seconds, "p50"),
            _seconds(scaling.lifetime_seconds, "p90"),
            _percent(scaling.occupancy, "p50"),
            _percent(scaling.occupancy, "p90"),
        )
        for scaling in scalings
    ]
    return ReportTable(
        step=1,
        title=f"How multi-cluster scaling behaved (last {days} days)",
        columns=list(_COLUMNS),
        rows=rows,
    )


def behavior_notes(scalings: list[WarehouseScaling]) -> list[str]:
    """Per-warehouse plain-language description lines to print under the table."""
    if not scalings:
        return ["", "No multi-cluster warehouses with cluster activity in this window."]
    return ["", "What the numbers show:", *(f"  {scaling.warehouse}: {scaling.behavior}" for scaling in scalings)]
