"""Turn the two rounds into a side-by-side comparison table and takeaway notes."""

from __future__ import annotations

from common.tables import ReportTable
from experiments.multicluster_demo.core.benchmark import RoundResult


def comparison_table(single: RoundResult, multi: RoundResult) -> ReportTable:
    """Single-cluster vs multi-cluster, metric by metric."""
    rows: list[tuple[object, ...]] = [
        ("clusters used", single.clusters_used, multi.clusters_used),
        ("queue time p50 (ms)", f"{single.queue_p50_ms:.0f}", f"{multi.queue_p50_ms:.0f}"),
        ("queue time p95 (ms)", f"{single.queue_p95_ms:.0f}", f"{multi.queue_p95_ms:.0f}"),
        ("total wall-clock (s)", f"{single.wall_clock_s:.1f}", f"{multi.wall_clock_s:.1f}"),
        ("queries", single.query_count, multi.query_count),
        ("failed", single.failures, multi.failures),
    ]
    return ReportTable(
        step=1,
        title=f"Single vs multi-cluster — {single.concurrency} concurrent queries",
        columns=["metric", "single cluster", multi.label],
        rows=rows,
    )


def _pct_drop(before: float, after: float) -> str:
    if before <= 0:
        return "n/a"
    return f"{(before - after) / before:.0%}"


def takeaways(single: RoundResult, multi: RoundResult) -> list[str]:
    """Plain-language summary of what multi-cluster did and the benefit."""
    lines = ["", "What multi-cluster did:"]
    lines.append(
        f"  Snowflake ran the same {single.concurrency} concurrent queries across "
        f"{multi.clusters_used} cluster(s) instead of {single.clusters_used}."
    )
    lines.append(
        f"  p95 queue time: {single.queue_p95_ms:.0f} ms -> {multi.queue_p95_ms:.0f} ms "
        f"({_pct_drop(single.queue_p95_ms, multi.queue_p95_ms)} lower)."
    )
    lines.append(
        f"  Total wall-clock to clear the batch: {single.wall_clock_s:.1f} s -> "
        f"{multi.wall_clock_s:.1f} s ({_pct_drop(single.wall_clock_s, multi.wall_clock_s)} faster)."
    )
    if multi.clusters_used <= 1:
        lines.append(
            "  NOTE: the multi-cluster round did not add a cluster — try more "
            "concurrency (--concurrency) so the single cluster actually queues."
        )
    return lines
