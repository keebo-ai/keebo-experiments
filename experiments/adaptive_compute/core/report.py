"""Turn a run into tables: runtimes now, bills and crossovers once ACCOUNT_USAGE catches up.

:func:`live_tables` and :func:`live_summary_lines` format what
``run.run_grid`` returned, the moment it finishes. Those are runtimes only,
because Adaptive's cost isn't knowable until Snowflake bills it.

:func:`read_report` reads the run back from ``ACCOUNT_USAGE``:

- **runtime** per cell from ``QUERY_HISTORY`` (first query's start to last one's end),
  split into compile, queue, and execution time, with spill,
- **billed credits** per cell from ``WAREHOUSE_METERING_HISTORY``, exact because
  every cell ran on a warehouse of its own,
- Adaptive's **per-query credits** from ``QUERY_METERING_HISTORY``, as a cross-check,

and works out, for each workload and size, where Adaptive stops being faster or
cheaper than Gen1 and Gen2 (see :class:`Comparison`), how monster-split
compares with monster, and which cells no other cell beats on both time and
cost (the frontier). Everything returns
:class:`~common.tables.ReportTable` values and lines; the CLI prints them. No
``click`` here.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from common import compare, dedicated, warehouses
from common.snowflake import rows
from common.tables import ReportTable
from experiments.adaptive_compute.core import infra, plan, queries
from experiments.adaptive_compute.core.run import RunResult

_MISSING = "—"

# Two measurements within this share of each other count as a tie. With one
# replicate, differences this small are inside the run-to-run noise.
TIE_BAND = 0.05

# The comparisons the report makes: (challenger, baseline).
PAIRS = ((queries.ADAPTIVE, queries.GEN1), (queries.ADAPTIVE, queries.GEN2), (queries.GEN2, queries.GEN1))


def _cell(value: float | None, digits: int, *, capped: bool = False) -> str:
    """Format a measurement; ``+`` marks a lower bound from a cell the cost cap stopped."""
    if value is None:
        return _MISSING
    return f"{value:.{digits}f}{'+' if capped else ''}"


def _size_order(size: str) -> int:
    return warehouses.SIZE_KEYWORDS.index(size)


def _workload_order(workload: str) -> int:
    return [w.key for w in queries.WORKLOADS].index(workload)


# --------------------------------------------------------------------------- #
# Live results (client clock)
# --------------------------------------------------------------------------- #
def live_tables(result: RunResult) -> list[ReportTable]:
    """Each cell's runtime, and the configurations side by side for each workload and size."""
    cells = ReportTable(
        step=None,
        title="Each cell (client clock; `+` = stopped by the cost cap, so a lower bound)",
        columns=["workload", "size", "config", "statements ok", "wall (s)", "est. credits"],
        rows=[
            (
                c.cell.workload.key,
                warehouses.SIZE_LABEL[c.cell.size],
                c.cell.config.label,
                f"{c.succeeded}/{c.statements}",
                _cell(c.wall_s, 1, capped=bool(c.capped)),
                "report"
                if c.estimated_credits is None
                else _cell(
                    c.estimated_credits, 4, capped=_lower_bound(c, result.max_clusters, result.split_max_clusters)
                ),
            )
            for c in result.cells
        ],
    )

    walls: dict[tuple[str, str], dict[str, list[float]]] = {}
    capped: set[tuple[str, str, str]] = set()
    for c in result.cells:
        walls.setdefault((c.cell.workload.key, c.cell.size), {}).setdefault(c.cell.config.key, []).append(c.wall_s)
        if c.capped:
            capped.add((c.cell.workload.key, c.cell.size, c.cell.config.key))
    side_rows = []
    for (workload, size), by_config in sorted(
        walls.items(), key=lambda kv: (_workload_order(kv[0][0]), _size_order(kv[0][1]))
    ):
        medians = {key: statistics.median(values) for key, values in by_config.items()}
        row: list[Any] = [workload, warehouses.SIZE_LABEL[size]]
        for config in queries.CONFIGS:
            row.append(_cell(medians.get(config.key), 1, capped=(workload, size, config.key) in capped))
        for challenger, baseline in PAIRS[:2]:
            row.append(_speed_text(medians.get(baseline.key), medians.get(challenger.key)) or _MISSING)
        side_rows.append(tuple(row))
    side = ReportTable(
        step=None,
        title="Runtime side by side (median wall seconds)",
        columns=["workload", "size", *(c.label for c in queries.CONFIGS), "Adaptive vs Gen1", "Adaptive vs Gen2"],
        rows=side_rows,
    )
    return [cells, side]


def _lower_bound(c: Any, max_clusters: int, split_max_clusters: int = queries.DEFAULT_SPLIT_MAX_CLUSTERS) -> bool:
    """A standard cell's estimate undercounts when it was capped or could have used extra clusters."""
    return bool(c.capped) or plan.clusters(c.cell, max_clusters=max_clusters, split_max_clusters=split_max_clusters) > 1


def live_summary_lines(result: RunResult) -> list[str]:
    standard = [c.estimated_credits for c in result.cells if c.estimated_credits is not None]
    lines = [
        f"Run {result.run_id}: {len(result.cells)} cells. The standard warehouses cost about "
        f"{sum(standard):.3f} credits by the client's clock (60-second minimum included; extra clusters not). "
        f"The cap counted at most {result.worst_case_credits:.3f} credits against --max-credits.",
        "What Adaptive cost, and so where it's cheaper, comes from Snowflake's bill. Run "
        f"`keebo-experiments adaptive-compute report --run-id {result.run_id}` in an hour or so; "
        "WAREHOUSE_METERING_HISTORY can take up to 3 hours to fill in.",
    ]
    if result.stopped_early:
        lines.append(f"The run stopped early: {result.stopped_early}")
    if any(c.capped for c in result.cells):
        lines.append("Some cells hit their limit (marked +). Raise --max-credits to let them finish.")
    if any(c.failed for c in result.cells):
        lines.append("Some statements failed (see the log above). The report counts them per cell.")
    if result.mismatched_answers:
        lines.append(
            "WARNING: these monster cells returned a different answer from the rest, so they didn't do the same "
            f"work: {', '.join(result.mismatched_answers)}."
        )
    return lines


def _speed_text(baseline_s: float | None, challenger_s: float | None) -> str | None:
    if not baseline_s or not challenger_s:
        return None
    if abs(baseline_s / challenger_s - 1) <= TIE_BAND:
        return "same speed"
    return compare.speed_change(baseline_s, challenger_s)


def _cost_text(baseline: float | None, challenger: float | None) -> str | None:
    if not baseline or not challenger:
        return None
    if abs(challenger / baseline - 1) <= TIE_BAND:
        return "about the same"
    return compare.cost_change(baseline, challenger)


# --------------------------------------------------------------------------- #
# ACCOUNT_USAGE
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CellStats:
    """One (workload, size, config), from ACCOUNT_USAGE, as the median over its replicates."""

    workload: str
    size: str
    config: queries.Config
    replicates: int
    statements: int
    failed: int
    distinct_hashes: int
    wall_s: float  # first query's start to the last one's end
    elapsed_s: float  # summed over queries
    queued_s: float
    clusters: int
    busy_s: float  # summed over clusters
    floor_s: float  # each cluster's busy time, 60 seconds at least, summed
    billed: float | None  # WAREHOUSE_METERING_HISTORY; None until it lands for every replicate
    query_metered: float | None  # QUERY_METERING_HISTORY (Adaptive only)
    # Where the time went, summed over the cell's statements. Compile time is in
    # cloud services, not on the warehouse's bill.
    compile_s: float = 0.0
    exec_s: float = 0.0
    provisioning_s: float = 0.0  # queued for the warehouse to start
    overload_s: float = 0.0  # queued for room on a busy warehouse
    spilled_local_gb: float = 0.0
    spilled_remote_gb: float = 0.0
    cloud_services_credits: float = 0.0  # per query, before the daily 10% adjustment

    @property
    def rate(self) -> float | None:
        """A standard warehouse's published credits per hour, per cluster. ``None`` for Adaptive."""
        if self.config.adaptive:
            return None
        return warehouses.credits_per_hour(self.size, self.config.generation or "1")

    @property
    def warm(self) -> float | None:
        """What the work costs on a warehouse that's already running, so with no minimum and no idle time.

        For a standard warehouse that's its busy cluster time at the published
        rate. Adaptive bills by the query, so for it this is just the bill.
        """
        if self.rate is None:
            return self.billed
        return self.busy_s * self.rate / 3600

    @property
    def modelled(self) -> float | None:
        """What a fresh standard warehouse *should* bill: each cluster's busy time, with its 60-second minimum."""
        return None if self.rate is None else self.floor_s * self.rate / 3600

    @property
    def effective_rate(self) -> float | None:
        """Billed credits per hour of query time: Adaptive's rate as it actually came out."""
        if self.billed is None or not self.elapsed_s:
            return None
        return self.billed * 3600 / self.elapsed_s


@dataclass(frozen=True)
class Comparison:
    """A challenger against a baseline on one workload and size.

    Usually the two are different configurations on the same workload. In a
    workload pair (:data:`queries.WORKLOAD_PAIRS`) they're the same
    configuration on two workloads, and ``baseline_workload`` names the
    baseline's.

    ``speedup`` is baseline runtime / challenger runtime, so above 1 the
    challenger is faster. ``fresh_ratio`` and ``warm_ratio`` are challenger
    cost / baseline cost, so below 1 the challenger is cheaper: "fresh" from
    each one's bill as a brand-new warehouse, "warm" for the work alone on a
    warehouse that's already running.

    When the challenger is Adaptive and the baseline is standard, there are two
    break-even points. Neither is measured directly; both are worked out from
    the measurements above.

    - ``breakeven_busy``: a standard warehouse that's kept running bills its
      busy time divided by the share of time it's busy, ``u``. It's cheaper
      than Adaptive when ``u`` is above ``warm(std) / billed(adaptive)``. At 1
      or more it never is, even flat out.
    - ``breakeven_auto_suspend_s``: run on its own, a standard warehouse bills
      the workload plus AUTO_SUSPEND seconds of idle time, less whatever of
      that idle time its 60-second minimum already covered. This is the
      AUTO_SUSPEND at which that matches Adaptive's bill. Above it, Adaptive is
      cheaper. 0 means Adaptive is cheaper even when the warehouse suspends
      the moment the work ends.
    """

    workload: str
    size: str
    challenger: queries.Config
    baseline: queries.Config
    speedup: float | None
    fresh_ratio: float | None
    warm_ratio: float | None
    breakeven_busy: float | None = None
    breakeven_auto_suspend_s: float | None = None
    baseline_workload: str | None = None  # None: the same as ``workload``

    @property
    def against(self) -> str:
        """The workload the baseline ran."""
        return self.baseline_workload or self.workload


def compare_cells(challenger: CellStats, baseline: CellStats) -> Comparison:
    """Work out one :class:`Comparison` from two cells of the same workload and size."""
    speedup = baseline.wall_s / challenger.wall_s if challenger.wall_s and baseline.wall_s else None
    fresh = challenger.billed / baseline.billed if challenger.billed and baseline.billed else None
    warm = challenger.warm / baseline.warm if challenger.warm and baseline.warm else None
    busy = auto_suspend = None
    if challenger.config.adaptive and baseline.rate is not None and challenger.billed and baseline.billed:
        busy = (baseline.warm or 0) / challenger.billed
        if challenger.billed <= baseline.billed:
            auto_suspend = 0.0
        else:
            slack_s = max(0.0, warehouses.BILLING_MINIMUM_SECONDS - baseline.wall_s)
            auto_suspend = slack_s + (challenger.billed - baseline.billed) * 3600 / baseline.rate
    return Comparison(
        workload=challenger.workload,
        size=challenger.size,
        challenger=challenger.config,
        baseline=baseline.config,
        speedup=speedup,
        fresh_ratio=fresh,
        warm_ratio=warm,
        breakeven_busy=busy,
        breakeven_auto_suspend_s=auto_suspend,
        baseline_workload=None if baseline.workload == challenger.workload else baseline.workload,
    )


@dataclass(frozen=True)
class ReportSummary:
    run_id: str
    cells: list[CellStats]
    comparisons: list[Comparison]
    tables: list[ReportTable]
    lines: list[str]
    workload_comparisons: list[Comparison] = field(default_factory=list)  # Step 2b: monster-split vs monster


def parse_tag(tag: str) -> tuple[str, str, str, int] | None:
    """``adcomp:<run>:<workload>:<size>:<config>:<replicate>`` -> (workload, size, config, replicate)."""
    parts = tag.split(":")
    if len(parts) != 6 or parts[0] != queries.QUERY_TAG_PREFIX:
        return None
    _, _, workload, size, config, replicate = parts
    if workload not in queries.WORKLOADS_BY_KEY or size not in warehouses.SIZE_KEYWORDS:
        return None
    if config not in queries.CONFIGS_BY_KEY or not replicate.isdigit():
        return None
    return workload, size, config, int(replicate)


def read_report(
    conn: Any,
    *,
    objects: infra.HomeObjects,
    hours: int = 48,
    run_id: str | None = None,
) -> ReportSummary | None:
    """Read one run back from ACCOUNT_USAGE on the home warehouse. ``None`` if no run shows up yet."""
    if int(hours) < 1:
        raise ValueError(f"hours must be at least 1, got {hours}")
    if run_id is not None:
        queries.validate_run_id(run_id)

    cur = conn.cursor()
    try:
        infra.require_warehouse(cur, objects)
        cur.execute(f"USE WAREHOUSE {objects.warehouse}")
        try:
            if run_id is None:
                cur.execute(queries.LATEST_RUN_SQL.format(home=objects.warehouse, hours=int(hours)))
                found = cur.fetchall()
                run_id = found[0][0] if found and found[0][0] else None
            if run_id is None:
                return None

            def fill(sql: str) -> str:
                return queries.report_sql(sql, home=objects.warehouse, run_id=run_id, hours=hours)

            cur.execute(fill(queries.CELLS_SQL))
            cell_rows = rows(cur)
            cur.execute(fill(queries.METERING_SQL))
            billed = {str(r["warehouse_name"]).upper(): float(r["billed_credits"]) for r in rows(cur)}
            metered_note = None
            try:
                cur.execute(fill(queries.QUERY_METERING_SQL))
                metered = {str(r["query_tag"]): float(r["query_credits"]) for r in rows(cur)}
            except Exception as exc:  # needs the USAGE_VIEWER role; the report works without it
                metered = {}
                metered_note = (
                    f"QUERY_METERING_HISTORY isn't readable ({exc}), so Adaptive's per-query check is skipped."
                )
        finally:
            dedicated.suspend_quietly(cur, objects.warehouse)
    finally:
        cur.close()

    cells = aggregate(cell_rows, billed=billed, metered=metered)
    comparisons = comparisons_for(cells)
    pairs = workload_comparisons_for(cells)
    lines = report_lines(cells, comparisons, pairs)
    if metered_note:
        lines.append(metered_note)
    return ReportSummary(
        run_id=run_id,
        cells=cells,
        comparisons=comparisons,
        tables=report_tables(cells, comparisons, pairs),
        lines=lines,
        workload_comparisons=pairs,
    )


def aggregate(
    cell_rows: Sequence[dict[str, Any]], *, billed: dict[str, float], metered: dict[str, float]
) -> list[CellStats]:
    """Fold the per-replicate rows into one :class:`CellStats` per (workload, size, config)."""
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in cell_rows:
        parsed = parse_tag(str(row["query_tag"]))
        if parsed is None:
            continue
        workload, size, config, _replicate = parsed
        enriched = dict(row)
        enriched["billed"] = billed.get(str(row["warehouse_name"]).upper())
        enriched["query_metered"] = metered.get(str(row["query_tag"]))
        grouped.setdefault((workload, size, config), []).append(enriched)

    def median(group: list[dict[str, Any]], column: str) -> float:
        return float(statistics.median(float(r[column] or 0) for r in group))

    def median_or_none(group: list[dict[str, Any]], column: str) -> float | None:
        values = [r[column] for r in group]
        return None if any(v is None for v in values) else float(statistics.median(values))

    cells = [
        CellStats(
            workload=workload,
            size=size,
            config=queries.CONFIGS_BY_KEY[config],
            replicates=len(group),
            statements=max(int(r["statements"]) for r in group),
            failed=sum(int(r["failed"] or 0) for r in group),
            distinct_hashes=max(int(r["distinct_hashes"] or 0) for r in group),
            wall_s=median(group, "wall_s"),
            elapsed_s=median(group, "elapsed_s"),
            queued_s=median(group, "queued_s"),
            clusters=max(int(r["clusters"] or 0) for r in group),
            busy_s=median(group, "busy_s"),
            floor_s=median(group, "floor_s"),
            billed=median_or_none(group, "billed"),
            query_metered=median_or_none(group, "query_metered"),
            compile_s=median(group, "compile_s"),
            exec_s=median(group, "exec_s"),
            provisioning_s=median(group, "provisioning_s"),
            overload_s=median(group, "overload_s"),
            spilled_local_gb=median(group, "spilled_local_gb"),
            spilled_remote_gb=median(group, "spilled_remote_gb"),
            cloud_services_credits=median(group, "cloud_services_credits"),
        )
        for (workload, size, config), group in grouped.items()
    ]
    return sorted(
        cells,
        key=lambda c: (_workload_order(c.workload), _size_order(c.size), queries.CONFIGS.index(c.config)),
    )


def comparisons_for(cells: Sequence[CellStats]) -> list[Comparison]:
    by_key = {(c.workload, c.size, c.config.key): c for c in cells}
    found = []
    for workload, size in dict.fromkeys((c.workload, c.size) for c in cells):
        for challenger, baseline in PAIRS:
            a, b = by_key.get((workload, size, challenger.key)), by_key.get((workload, size, baseline.key))
            if a is not None and b is not None:
                found.append(compare_cells(a, b))
    return found


def workload_comparisons_for(cells: Sequence[CellStats]) -> list[Comparison]:
    """Each workload pair (monster-split against monster) on the same size and configuration."""
    by_key = {(c.workload, c.size, c.config.key): c for c in cells}
    found = []
    for challenger, baseline in queries.WORKLOAD_PAIRS:
        for c in cells:
            if c.workload != challenger.key:
                continue
            other = by_key.get((baseline.key, c.size, c.config.key))
            if other is not None:
                found.append(compare_cells(c, other))
    return found


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #
def report_tables(
    cells: Sequence[CellStats], comparisons: Sequence[Comparison], pairs: Sequence[Comparison] = ()
) -> list[ReportTable]:
    tables = [_cells_table(cells), _head_to_head_table(comparisons)]
    if pairs:
        tables.append(_workload_pairs_table(pairs))
    return [*tables, _checks_table(cells), _frontier_table(cells)]


def _cells_table(cells: Sequence[CellStats]) -> ReportTable:
    return ReportTable(
        step=1,
        title="Each cell, from ACCOUNT_USAGE (median over replicates)",
        columns=[
            "workload",
            "size",
            "config",
            "reps",
            "ok/stmts",
            "wall (s)",
            "queued (s)",
            "clusters",
            "compile (s)",
            "exec (s)",
            "spill local GB",
            "spill remote GB",
            "billed cr",
            "warm cr",
            "cr/hr of query time",
        ],
        rows=[
            (
                c.workload,
                warehouses.SIZE_LABEL[c.size],
                c.config.label,
                c.replicates,
                f"{c.statements - c.failed}/{c.statements}",
                _cell(c.wall_s, 1),
                _cell(c.queued_s, 1),
                c.clusters if not c.config.adaptive else _MISSING,
                _cell(c.compile_s, 1),
                _cell(c.exec_s, 1),
                _cell(c.spilled_local_gb, 2),
                _cell(c.spilled_remote_gb, 2),
                _cell(c.billed, 4),
                _cell(c.warm, 4),
                _cell(c.effective_rate, 2),
            )
            for c in cells
        ],
    )


def _head_to_head_table(comparisons: Sequence[Comparison]) -> ReportTable:
    return ReportTable(
        step=2,
        title="Head to head: the challenger next to the baseline (where the lines cross)",
        columns=[
            "workload",
            "size",
            "challenger vs baseline",
            "speed",
            "cost, fresh warehouse",
            "cost, warm warehouse",
            "std is cheaper when busy >",
            "Adaptive is cheaper when AUTO_SUSPEND >",
        ],
        rows=[
            (
                c.workload,
                warehouses.SIZE_LABEL[c.size],
                f"{c.challenger.label} vs {c.baseline.label}",
                _ratio_speed(c.speedup),
                _ratio_cost(c.fresh_ratio),
                _ratio_cost(c.warm_ratio),
                _busy_text(c),
                _auto_suspend_text(c),
            )
            for c in comparisons
        ],
    )


def _workload_pairs_table(pairs: Sequence[Comparison]) -> ReportTable:
    """Step 2b: is the advantage parallelism inside one statement, or concurrency across statements?"""
    return ReportTable(
        step=None,
        title="Step 2b. Split vs one statement: the same logic, as concurrent statements or as one",
        columns=["workload", "size", "config", "speed", "cost, fresh warehouse", "cost, warm warehouse"],
        rows=[
            (
                f"{c.workload} vs {c.against}",
                warehouses.SIZE_LABEL[c.size],
                c.challenger.label,
                _ratio_speed(c.speedup),
                _ratio_cost(c.fresh_ratio),
                _ratio_cost(c.warm_ratio),
            )
            for c in pairs
        ],
    )


def frontier(cells: Sequence[CellStats]) -> set[CellStats]:
    """The cells no other cell of the same workload beats on both runtime and bill.

    A cell is off the frontier when another one ran no slower *and* billed no
    more, and did better on at least one. Cells with no bill yet, or with
    failed statements (they did less work), aren't counted either way.
    """
    known = [c for c in cells if c.billed is not None and not c.failed]
    on = set()
    for c in known:
        rivals = [o for o in known if o.workload == c.workload and o is not c]
        beaten = any(
            o.wall_s <= c.wall_s and o.billed <= c.billed and (o.wall_s < c.wall_s or o.billed < c.billed)
            for o in rivals
        )
        if not beaten:
            on.add(c)
    return on


def _frontier_table(cells: Sequence[CellStats]) -> ReportTable:
    """Step 4: for each workload, every size and ceiling by cost, and which are worth running at all."""
    best = frontier(cells)
    ordered = sorted(
        (c for c in cells if c.billed is not None),
        key=lambda c: (_workload_order(c.workload), c.billed, c.wall_s),
    )
    return ReportTable(
        step=4,
        title="Time vs credits: the frontier (no other cell of the workload is both faster and cheaper)",
        columns=["workload", "size", "config", "wall (s)", "billed cr", "on the frontier"],
        rows=[
            (
                c.workload,
                warehouses.SIZE_LABEL[c.size],
                c.config.label,
                _cell(c.wall_s, 1),
                _cell(c.billed, 4),
                "yes" if c in best else "",
            )
            for c in ordered
        ],
    )


def _ratio_speed(speedup: float | None) -> str:
    return _MISSING if speedup is None else (_speed_text(speedup, 1.0) or _MISSING)


def _ratio_cost(ratio: float | None) -> str:
    return _MISSING if ratio is None else (_cost_text(1.0, ratio) or _MISSING)


def _busy_text(c: Comparison) -> str:
    if c.breakeven_busy is None:
        return _MISSING if c.challenger.adaptive else ""
    return "never" if c.breakeven_busy >= 1 else f"{c.breakeven_busy:.0%}"


def _auto_suspend_text(c: Comparison) -> str:
    if c.breakeven_auto_suspend_s is None:
        return _MISSING if c.challenger.adaptive else ""
    return "any (0s)" if c.breakeven_auto_suspend_s == 0 else f"{c.breakeven_auto_suspend_s:.0f}s"


def _checks_table(cells: Sequence[CellStats]) -> ReportTable:
    """Does the bill match what we think the billing rules are? If not, distrust the crossovers.

    The note flags a cell that ran a different number of distinct statements
    from the rest of its workload, which means it didn't run the same work.
    """
    usual: dict[str, int] = {}
    for workload in {c.workload for c in cells}:
        counts = [c.distinct_hashes for c in cells if c.workload == workload]
        usual[workload] = statistics.mode(counts)
    table_rows = []
    for c in cells:
        if c.config.adaptive:
            check, model = "per-query metering / warehouse metering", c.query_metered
        else:
            check, model = "billed / (busy per cluster, 60s min, x rate)", c.modelled
        ratio = c.billed / model if c.billed and model else None
        table_rows.append(
            (
                c.workload,
                warehouses.SIZE_LABEL[c.size],
                c.config.label,
                check,
                _cell(c.billed, 4),
                _cell(model, 4),
                _cell(ratio, 2),
                ""
                if c.distinct_hashes == usual[c.workload]
                else f"{c.distinct_hashes} distinct statements, others ran {usual[c.workload]}",
            )
        )
    return ReportTable(
        step=3,
        title="Measurement checks (ratios near 1.00 mean the bill is what the billing rules predict)",
        columns=["workload", "size", "config", "check", "billed cr", "expected cr", "ratio", "note"],
        rows=table_rows,
    )


# --------------------------------------------------------------------------- #
# The findings, in words
# --------------------------------------------------------------------------- #
def report_lines(
    cells: Sequence[CellStats], comparisons: Sequence[Comparison], pairs: Sequence[Comparison] = ()
) -> list[str]:
    """Where the lines cross, one sentence per workload and comparison, plus what's still missing."""
    lines: list[str] = []
    missing = [c for c in cells if c.billed is None]
    if missing:
        lines.append(
            f"{len(missing)} of {len(cells)} cells have no metering yet, so their costs show as {_MISSING}. "
            "WAREHOUSE_METERING_HISTORY can take up to 3 hours; rerun the report later."
        )
    failed = [c for c in cells if c.failed]
    if failed:
        lines.append(
            f"{len(failed)} cells had failed or cancelled statements (ok/stmts in Step 1). Their runtimes and costs "
            "cover less work, so compare them with care."
        )
    if cells and max(c.replicates for c in cells) == 1:
        lines.append(
            f"One replicate per cell: treat differences under about {TIE_BAND:.0%} as noise, and rerun with "
            "--replicates 3 before quoting a number."
        )

    by_pair: dict[tuple[str, str, str], list[Comparison]] = {}
    for c in comparisons:
        by_pair.setdefault((c.workload, c.challenger.key, c.baseline.key), []).append(c)
    for (workload, _a, _b), group in by_pair.items():
        group = sorted(group, key=lambda c: _size_order(c.size))
        challenger, baseline = group[0].challenger.label, group[0].baseline.label
        parts = [
            _sizes_sentence("faster", "slower", "the same speed", [(c.size, c.speedup, True) for c in group]),
            _sizes_sentence(
                "cheaper as a fresh warehouse",
                "dearer as a fresh warehouse",
                "the same cost as a fresh warehouse",
                [(c.size, c.fresh_ratio, False) for c in group],
            ),
        ]
        if group[0].challenger.adaptive:
            parts.append(_busy_sentence(baseline, group))
            parts.append(_auto_suspend_sentence(baseline, group))
        lines.append(f"{workload}, {challenger} vs {baseline}: " + "; ".join(p for p in parts if p) + ".")

    by_config: dict[tuple[str, str, str], list[Comparison]] = {}
    for c in pairs:
        by_config.setdefault((c.workload, c.against, c.challenger.key), []).append(c)
    for (workload, against, _config), group in by_config.items():
        group = sorted(group, key=lambda c: _size_order(c.size))
        parts = [
            _sizes_sentence("faster", "slower", "the same speed", [(c.size, c.speedup, True) for c in group]),
            _sizes_sentence(
                "cheaper as a fresh warehouse",
                "dearer as a fresh warehouse",
                "the same cost as a fresh warehouse",
                [(c.size, c.fresh_ratio, False) for c in group],
            ),
        ]
        sentence = "; ".join(p for p in parts if p)
        if sentence:
            lines.append(f"{workload} vs {against}, on {group[0].challenger.label}: {sentence}.")

    compile_bound = [c for c in cells if c.compile_s and c.exec_s and c.compile_s >= 0.25 * c.exec_s]
    if compile_bound:
        lines.append(
            f"{len(compile_bound)} cells spent at least a quarter as long compiling as executing (Step 1). Compiling "
            "runs in cloud services, so it adds to the runtime but not to the warehouse's bill."
        )
    return lines


def _sizes_sentence(better: str, worse: str, even: str, values: list[tuple[str, float | None, bool]]) -> str:
    """'faster at X-Small, Small, slower at Medium', from (size, ratio, higher_is_better) triples.

    A ratio is a speedup (higher is better) or a cost ratio (lower is better).
    Within :data:`TIE_BAND` of 1 counts as even.
    """
    wins, losses, ties = [], [], []
    for size, ratio, higher_is_better in values:
        if ratio is None:
            continue
        label = warehouses.SIZE_LABEL[size]
        if abs(ratio - 1) <= TIE_BAND:
            ties.append(label)
        elif (ratio > 1) == higher_is_better:
            wins.append(label)
        else:
            losses.append(label)
    pieces = []
    for words, sizes in ((better, wins), (worse, losses), (even, ties)):
        if sizes:
            pieces.append(f"{words} at {', '.join(sizes)}")
    return ", ".join(pieces)


def _busy_sentence(baseline: str, group: Sequence[Comparison]) -> str:
    """'a Gen1 warehouse that's kept running beats it above 50% busy at X-Small, and never at Medium'."""
    above = [f"{c.breakeven_busy:.0%} busy at {warehouses.SIZE_LABEL[c.size]}" for c in group
             if c.breakeven_busy is not None and c.breakeven_busy < 1]  # fmt: skip
    never = [warehouses.SIZE_LABEL[c.size] for c in group if c.breakeven_busy is not None and c.breakeven_busy >= 1]
    pieces = []
    if above:
        pieces.append("above " + ", ".join(above))
    if never:
        pieces.append(f"never at {', '.join(never)}, even flat out")
    return f"a {baseline} warehouse that's kept running beats it " + ", and ".join(pieces) if pieces else ""


def _auto_suspend_sentence(baseline: str, group: Sequence[Comparison]) -> str:
    """'run on its own, it's cheaper than Gen1 at any AUTO_SUSPEND at X-Small, and above 18s at Medium'."""
    known = [c for c in group if c.breakeven_auto_suspend_s is not None]
    always = [warehouses.SIZE_LABEL[c.size] for c in known if c.breakeven_auto_suspend_s == 0]
    above = [f"{c.breakeven_auto_suspend_s:.0f}s at {warehouses.SIZE_LABEL[c.size]}" for c in known
             if c.breakeven_auto_suspend_s]  # fmt: skip
    pieces = []
    if always:
        pieces.append(f"at any AUTO_SUSPEND at {', '.join(always)}")
    if above:
        pieces.append("once AUTO_SUSPEND is above " + ", ".join(above))
    return f"run on its own, it's cheaper than {baseline} " + ", and ".join(pieces) if pieces else ""


# --------------------------------------------------------------------------- #
# CSV export, for whoever picks the analysis up next
# --------------------------------------------------------------------------- #
CELL_CSV_COLUMNS = [
    "run_id",
    "workload",
    "size",
    "config",
    "replicates",
    "statements",
    "failed",
    "wall_s",
    "elapsed_s",
    "queued_s",
    "clusters",
    "busy_s",
    "published_credits_per_hour",
    "billed_credits",
    "warm_credits",
    "modelled_credits",
    "query_metered_credits",
    "compile_s",
    "exec_s",
    "queued_provisioning_s",
    "queued_overload_s",
    "spilled_local_gb",
    "spilled_remote_gb",
    "cloud_services_credits",
    "on_frontier",
]
COMPARISON_CSV_COLUMNS = [
    "run_id",
    "workload",
    "size",
    "challenger",
    "baseline",
    "baseline_workload",
    "speedup",
    "fresh_cost_ratio",
    "warm_cost_ratio",
    "breakeven_busy_share",
    "breakeven_auto_suspend_s",
]


def csv_rows(summary: ReportSummary) -> tuple[list[list[Any]], list[list[Any]]]:
    """The cells and the comparisons as plain rows, matching the column lists above."""
    best = frontier(summary.cells)
    cells = [
        [
            summary.run_id,
            c.workload,
            c.size,
            c.config.key,
            c.replicates,
            c.statements,
            c.failed,
            round(c.wall_s, 3),
            round(c.elapsed_s, 3),
            round(c.queued_s, 3),
            c.clusters,
            round(c.busy_s, 3),
            c.rate,
            c.billed,
            None if c.warm is None else round(c.warm, 6),
            None if c.modelled is None else round(c.modelled, 6),
            c.query_metered,
            round(c.compile_s, 3),
            round(c.exec_s, 3),
            round(c.provisioning_s, 3),
            round(c.overload_s, 3),
            round(c.spilled_local_gb, 3),
            round(c.spilled_remote_gb, 3),
            round(c.cloud_services_credits, 6),
            None if c.billed is None else c in best,
        ]
        for c in summary.cells
    ]
    comparisons = [
        [
            summary.run_id,
            c.workload,
            c.size,
            c.challenger.key,
            c.baseline.key,
            c.against,
            None if c.speedup is None else round(c.speedup, 4),
            None if c.fresh_ratio is None else round(c.fresh_ratio, 4),
            None if c.warm_ratio is None else round(c.warm_ratio, 4),
            None if c.breakeven_busy is None else round(c.breakeven_busy, 4),
            None if c.breakeven_auto_suspend_s is None else round(c.breakeven_auto_suspend_s, 1),
        ]
        for c in [*summary.comparisons, *summary.workload_comparisons]
    ]
    return cells, comparisons
