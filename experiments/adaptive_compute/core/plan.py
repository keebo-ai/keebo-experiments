"""The grid of cells a run measures, the order it runs them in, and the cost cap.

A **cell** is one workload on one size of one warehouse configuration (one
replicate of it). Each cell runs on a warehouse created for it alone, so what
Snowflake bills that warehouse is exactly what the cell cost.

Pure planning and arithmetic, with no database or CLI dependencies.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from common import warehouses
from experiments.adaptive_compute.core import queries


@dataclass(frozen=True)
class Cell:
    """One workload, on one size, of one configuration."""

    workload: queries.Workload
    size: str  # a size keyword; for Adaptive, the MAX_QUERY_PERFORMANCE_LEVEL
    config: queries.Config
    replicate: int = 1

    @property
    def key(self) -> str:
        """Unique within a run; ends both the cell's warehouse name and its scratch table's."""
        return f"{self.config.code}_{self.size}_{self.workload.code}_{self.replicate}"

    @property
    def label(self) -> str:
        return f"{self.workload.key} / {warehouses.SIZE_LABEL[self.size]} / {self.config.label}"

    def warehouse(self, home: str, run_id: str) -> str:
        """The cell's own warehouse. It starts with the home warehouse's name, which is how cleanup finds it."""
        return f"{home}_{run_id}_{self.key}"

    def table(self, database: str, run_id: str) -> str:
        """The cell's scratch table, for the workloads that write."""
        return f"{database}.PUBLIC.T_{run_id}_{self.key}"

    def tag(self, run_id: str) -> str:
        return f"{queries.QUERY_TAG_PREFIX}:{run_id}:{self.workload.key}:{self.size}:{self.config.key}:{self.replicate}"


def build_plan(
    *,
    configs: Sequence[queries.Config],
    sizes: Sequence[str],
    workloads: Sequence[queries.Workload],
    replicates: int = 1,
) -> list[Cell]:
    """Every cell, in the order the run measures them.

    Cells run one at a time, a workload at a time, smallest size first. Within
    each (workload, size) the configurations take turns going first, so none of
    them is always the one that runs right after the account has warmed up, or
    right before it gets busy.
    """
    if replicates < 1:
        raise ValueError(f"replicates must be at least 1, got {replicates}")
    if not configs or not sizes or not workloads:
        raise ValueError("pick at least one configuration, size, and workload")
    for size in sizes:
        if size not in warehouses.SIZE_KEYWORDS:
            raise ValueError(f"size must be one of {', '.join(warehouses.SIZE_KEYWORDS)}, got {size!r}")
    ordered_sizes = [keyword for keyword in warehouses.SIZE_KEYWORDS if keyword in sizes]

    cells: list[Cell] = []
    turn = 0
    for replicate in range(1, replicates + 1):
        for workload in workloads:
            for size in ordered_sizes:
                rotated = [configs[(turn + i) % len(configs)] for i in range(len(configs))]
                cells.extend(Cell(workload, size, config, replicate) for config in rotated)
                turn += 1
    return cells


# --------------------------------------------------------------------------- #
# The cost cap
# --------------------------------------------------------------------------- #
def worst_case_rate(cell: Cell, *, max_clusters: int, throughput_multiplier: int) -> float:
    """The most credits per hour the cell's warehouse could burn.

    A standard warehouse bills its size's rate per running cluster, and only the
    concurrent workload can start more than one. Snowflake doesn't publish a
    rate for Adaptive, so the cap *assumes* it bills no faster than a Gen2
    warehouse at its performance level, times the throughput multiplier for a
    burst (8 when the multiplier is 0, meaning unlimited). The report checks
    that assumption against the real bill.
    """
    if cell.config.adaptive:
        burst = (throughput_multiplier or 8) if cell.workload.concurrent else 1
        return warehouses.credits_per_hour(cell.size, "2") * burst
    clusters = max_clusters if cell.workload.concurrent else 1
    return warehouses.credits_per_hour(cell.size, cell.config.generation or "1") * clusters


def minimum_credits(cell: Cell) -> float:
    """What the cell costs even if it does nothing: a standard warehouse's 60-second minimum."""
    if cell.config.adaptive:
        return 0.0
    rate = warehouses.credits_per_hour(cell.size, cell.config.generation or "1")
    return rate * warehouses.BILLING_MINIMUM_SECONDS / 3600


def worst_case_credits(cell: Cell, wall_s: float, *, max_clusters: int, throughput_multiplier: int) -> float:
    """The most the cell can cost if its warehouse runs flat out for ``wall_s`` (60 seconds at least)."""
    rate = worst_case_rate(cell, max_clusters=max_clusters, throughput_multiplier=throughput_multiplier)
    billed_s = wall_s if cell.config.adaptive else max(wall_s, warehouses.BILLING_MINIMUM_SECONDS)
    return rate * billed_s / 3600


def wall_limit(
    cell: Cell,
    *,
    budget: float,
    cells_left: int,
    max_clusters: int,
    throughput_multiplier: int,
) -> int:
    """How long ``cell`` may run so that every cell still to go, this one included, fits in ``budget``.

    The budget is split evenly across the cells left, and the cell's share,
    divided by its worst-case rate, becomes its wall-clock limit: no statement
    runs past it, and the run cancels anything still going when it hits.
    Spread evenly, a small warehouse gets a long limit and a big one a short
    one, which is how their runtimes tend to go anyway. ``run`` recomputes it
    before every cell, so what earlier cells didn't use goes to later ones.

    Raises ``ValueError`` if the share can't buy :data:`queries.MIN_WALL_SECONDS`.
    """
    share = budget / max(cells_left, 1)
    rate = worst_case_rate(cell, max_clusters=max_clusters, throughput_multiplier=throughput_multiplier)
    wall = int(share * 3600 / rate)
    if wall < queries.MIN_WALL_SECONDS:
        raise ValueError(
            f"--max-credits leaves {share:.3f} credits for {cell.label}, which buys it only {wall}s at its "
            f"worst-case rate of {rate:g} credits/hour. Raise --max-credits, or run fewer cells "
            "(--size, --workload, --config, --replicates)."
        )
    return min(wall, queries.MAX_WALL_SECONDS)


@dataclass(frozen=True)
class PlanSummary:
    """What a run will do, before it does it: for the confirmation prompt."""

    cells: int
    standard_cells: int
    adaptive_cells: int
    minimum_credits: float  # the standard warehouses' 60-second minimums alone
    max_credits: float
    tightest_wall_s: int
    tightest_label: str


def summarize(
    cells: Sequence[Cell], *, max_credits: float, max_clusters: int, throughput_multiplier: int
) -> PlanSummary:
    """Check the settings and that the cap covers every cell, and say what the run will cost at least and at most."""
    if max_clusters < 1:
        raise ValueError(f"max clusters must be at least 1, got {max_clusters}")
    if throughput_multiplier < 0 or throughput_multiplier == 1:
        raise ValueError(f"throughput multiplier must be 0 (unlimited) or 2 or more, got {throughput_multiplier}")
    if not cells:
        raise ValueError("there are no cells to run")
    walls = {
        cell: wall_limit(
            cell,
            budget=max_credits,
            cells_left=len(cells),
            max_clusters=max_clusters,
            throughput_multiplier=throughput_multiplier,
        )
        for cell in cells
    }
    tightest = min(walls, key=walls.get)
    return PlanSummary(
        cells=len(cells),
        standard_cells=sum(1 for cell in cells if not cell.config.adaptive),
        adaptive_cells=sum(1 for cell in cells if cell.config.adaptive),
        minimum_credits=round(sum(minimum_credits(cell) for cell in cells), 3),
        max_credits=max_credits,
        tightest_wall_s=walls[tightest],
        tightest_label=tightest.label,
    )
