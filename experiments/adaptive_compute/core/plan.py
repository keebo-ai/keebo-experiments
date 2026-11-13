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
    adaptive_ceilings: Sequence[str] = (),
) -> list[Cell]:
    """Every cell, in the order the run measures them.

    Cells run one at a time, a workload at a time, smallest size first. Within
    each (workload, size) the configurations take turns going first, so none of
    them is always the one that runs right after the account has warmed up, or
    right before it gets busy.

    ``adaptive_ceilings`` adds Adaptive-only cells at those
    MAX_QUERY_PERFORMANCE_LEVELs, after each workload's sizes: the sweep of
    Adaptive's ceiling above the standard sizes. A ceiling that's already one
    of the sizes, with Adaptive among the configurations, isn't run twice.
    """
    if replicates < 1:
        raise ValueError(f"replicates must be at least 1, got {replicates}")
    if not configs or not sizes or not workloads:
        raise ValueError("pick at least one configuration, size, and workload")
    for size in (*sizes, *adaptive_ceilings):
        if size not in warehouses.SIZE_KEYWORDS:
            raise ValueError(f"size must be one of {', '.join(warehouses.SIZE_KEYWORDS)}, got {size!r}")
    ordered_sizes = [keyword for keyword in warehouses.SIZE_KEYWORDS if keyword in sizes]
    ceilings = [
        keyword
        for keyword in warehouses.SIZE_KEYWORDS
        if keyword in adaptive_ceilings and not (keyword in sizes and queries.ADAPTIVE in configs)
    ]

    cells: list[Cell] = []
    turn = 0
    for replicate in range(1, replicates + 1):
        for workload in workloads:
            for size in ordered_sizes:
                rotated = [configs[(turn + i) % len(configs)] for i in range(len(configs))]
                cells.extend(Cell(workload, size, config, replicate) for config in rotated)
                turn += 1
            cells.extend(Cell(workload, ceiling, queries.ADAPTIVE, replicate) for ceiling in ceilings)
    return cells


# --------------------------------------------------------------------------- #
# The cost cap
# --------------------------------------------------------------------------- #
def clusters(cell: Cell, *, max_clusters: int, split_max_clusters: int = queries.DEFAULT_SPLIT_MAX_CLUSTERS) -> int:
    """MAX_CLUSTER_COUNT for a standard cell's warehouse.

    Only workloads that submit statements at once can use more than one
    cluster: ``max_clusters`` for the concurrent workload, and
    ``split_max_clusters`` for monster-split.
    """
    if not cell.workload.concurrent:
        return 1
    return split_max_clusters if cell.workload.staged else max_clusters


# Snowflake publishes no rate for Adaptive, so the cap bounds it from measured
# bills (SF10 and SF100 runs, 2026). One statement billed up to 1.65x the Gen2
# rate of its MAX_QUERY_PERFORMANCE_LEVEL, so the cap allows
# ADAPTIVE_SINGLE_HEADROOM times that rate. Statements submitted at once billed
# well past that rate times QUERY_THROUGHPUT_MULTIPLIER (up to 6x it), so the
# multiplier doesn't bound the bill. They stayed under the Gen2 rate times the
# number of statements submitted at once, which is what the cap allows.
ADAPTIVE_SINGLE_HEADROOM = 2.0
# An Adaptive warehouse that runs anything bills at least about 60 seconds at
# 1 credit/hour: short SF10 cells billed exactly this whatever they did. A
# short burst of statements submitted at once billed up to one of these per
# statement. Snowflake doesn't document either.
ADAPTIVE_MINIMUM_CREDITS = 1 / 60


def statements_at_once(cell: Cell, *, fanout: int = queries.DEFAULT_FANOUT) -> int:
    """The most statements the cell submits together: 22 TPC-H queries, monster-split's branches, or 1."""
    if not cell.workload.concurrent:
        return 1
    return fanout if cell.workload.staged else len(queries.TPCH)


def worst_case_rate(
    cell: Cell,
    *,
    max_clusters: int,
    split_max_clusters: int = queries.DEFAULT_SPLIT_MAX_CLUSTERS,
    fanout: int = queries.DEFAULT_FANOUT,
) -> float:
    """The most credits per hour the cell's warehouse could burn.

    A standard warehouse bills its size's rate per running cluster (see
    :func:`clusters`). For Adaptive, the cap allows a Gen2 warehouse's rate at
    its performance level per statement submitted at once, or
    :data:`ADAPTIVE_SINGLE_HEADROOM` times that rate for a lone statement.
    That's a bound from measured bills, not a published rate. The report's
    Step 3 shows the rate it actually billed at.
    """
    if cell.config.adaptive:
        at_once = statements_at_once(cell, fanout=fanout)
        return warehouses.credits_per_hour(cell.size, "2") * (at_once if at_once > 1 else ADAPTIVE_SINGLE_HEADROOM)
    count = clusters(cell, max_clusters=max_clusters, split_max_clusters=split_max_clusters)
    return warehouses.credits_per_hour(cell.size, cell.config.generation or "1") * count


def minimum_credits(cell: Cell) -> float:
    """What the cell costs even if it does nothing.

    A standard warehouse's 60-second minimum, or Adaptive's measured floor
    (:data:`ADAPTIVE_MINIMUM_CREDITS`).
    """
    if cell.config.adaptive:
        return ADAPTIVE_MINIMUM_CREDITS
    rate = warehouses.credits_per_hour(cell.size, cell.config.generation or "1")
    return rate * warehouses.BILLING_MINIMUM_SECONDS / 3600


def worst_case_credits(
    cell: Cell,
    wall_s: float,
    *,
    max_clusters: int,
    split_max_clusters: int = queries.DEFAULT_SPLIT_MAX_CLUSTERS,
    fanout: int = queries.DEFAULT_FANOUT,
) -> float:
    """The most the cell can cost if its warehouse runs flat out for ``wall_s``.

    Never less than its minimum, and for Adaptive never less than one floor per
    statement submitted at once, which is what a short burst has billed.
    """
    rate = worst_case_rate(cell, max_clusters=max_clusters, split_max_clusters=split_max_clusters, fanout=fanout)
    if cell.config.adaptive:
        floor = ADAPTIVE_MINIMUM_CREDITS * statements_at_once(cell, fanout=fanout)
        return max(rate * wall_s / 3600, floor)
    return rate * max(wall_s, warehouses.BILLING_MINIMUM_SECONDS) / 3600


def weight_of(cells: Sequence[Cell]) -> float:
    """The cells' combined claim on the cost cap (see :attr:`queries.Workload.weight`)."""
    return sum(cell.workload.weight for cell in cells)


def max_wall(cell: Cell) -> int:
    """The longest the cell may run, however much budget is left."""
    return cell.workload.max_wall_s or queries.MAX_WALL_SECONDS


def wall_limit(
    cell: Cell,
    *,
    budget: float,
    weight_left: float,
    max_clusters: int,
    split_max_clusters: int = queries.DEFAULT_SPLIT_MAX_CLUSTERS,
    fanout: int = queries.DEFAULT_FANOUT,
) -> int:
    """How long ``cell`` may run so that every cell still to go, this one included, fits in ``budget``.

    The budget is split across the cells left by their workload's weight
    (``weight_left`` is :func:`weight_of` those cells), and the cell's share,
    divided by its worst-case rate, becomes its wall-clock limit: no statement
    runs past it, and the run cancels anything still going when it hits.
    Every select.dev workload weighs 1, so without a monster workload that's an
    even split per cell. Spread that way, a small warehouse gets a long limit
    and a big one a short one, which is how their runtimes tend to go anyway.
    ``run`` recomputes it before every cell, so what earlier cells didn't use
    goes to later ones.

    Raises ``ValueError`` if the share can't buy :data:`queries.MIN_WALL_SECONDS`.
    """
    share = budget * cell.workload.weight / max(weight_left, cell.workload.weight)
    rate = worst_case_rate(cell, max_clusters=max_clusters, split_max_clusters=split_max_clusters, fanout=fanout)
    wall = int(share * 3600 / rate)
    if wall < queries.MIN_WALL_SECONDS:
        raise ValueError(
            f"--max-credits leaves {share:.3f} credits for {cell.label}, which buys it only {wall}s at its "
            f"worst-case rate of {rate:g} credits/hour. Raise --max-credits, or run fewer cells "
            "(--size, --workload, --config, --replicates, --adaptive-ceiling)."
        )
    return min(wall, max_wall(cell))


@dataclass(frozen=True)
class PlanSummary:
    """What a run will do, before it does it: for the confirmation prompt."""

    cells: int
    standard_cells: int
    adaptive_cells: int
    minimum_credits: float  # standard 60-second minimums plus Adaptive's measured floors
    max_credits: float
    tightest_wall_s: int
    tightest_label: str


def summarize(
    cells: Sequence[Cell],
    *,
    max_credits: float,
    max_clusters: int,
    throughput_multiplier: int,
    split_max_clusters: int = queries.DEFAULT_SPLIT_MAX_CLUSTERS,
    fanout: int = queries.DEFAULT_FANOUT,
) -> PlanSummary:
    """Check the settings and that the cap covers every cell, and say what the run will cost at least and at most."""
    if max_clusters < 1 or split_max_clusters < 1:
        raise ValueError(f"max clusters must be at least 1, got {min(max_clusters, split_max_clusters)}")
    if throughput_multiplier < 0 or throughput_multiplier == 1:
        raise ValueError(f"throughput multiplier must be 0 (unlimited) or 2 or more, got {throughput_multiplier}")
    if not cells:
        raise ValueError("there are no cells to run")
    total = weight_of(cells)
    walls = {
        cell: wall_limit(
            cell,
            budget=max_credits,
            weight_left=total,
            max_clusters=max_clusters,
            split_max_clusters=split_max_clusters,
            fanout=fanout,
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
