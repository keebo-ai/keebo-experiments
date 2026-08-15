"""Read the bill back and decide.

Takes an open connection and a manifest and returns structured results; the CLI
formats them. No ``click`` here.

Everything read here is scoped to this run's warehouses and time window, which
is why the run stamps a token into each warehouse name: metering rows are keyed
by warehouse name and bucketed hourly, so two runs sharing a name in the same
hour would sum into one row.

The order below is the whole point of the module, and must not be rearranged:

    metering rows -> decode at the published rate -> confirm every bill lands on
    a whole second -> confirm `control` was billed the time it ran -> only then
    read the multi-cluster numbers

So the published rate decodes the bills, and `control` checks the decoding
rather than defining it. The whole-second test is the strong form of that check:
at the right rate every bill is an exact whole number of seconds, and a rate
wrong by even half a percent pushes them off it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from experiments.multi_cluster_billing.core import events, manifest, queries, verdict


@dataclass(frozen=True)
class ReportTable:
    """One reporting query's result: its step number, title, and rows."""

    step: int
    title: str
    columns: list[str]
    rows: list[tuple[Any, ...]]


@dataclass(frozen=True)
class ReportSummary:
    """Everything `report` prints: the tables, the checks, and the verdict."""

    tables: list[ReportTable]
    meter_check: verdict.MeterCheck | None
    minimum_check: verdict.MinimumCheck | None
    verdict: verdict.Verdict | None
    not_ready_reason: str | None


def _quote_list(values: list[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _window(run: manifest.RunManifest) -> tuple[str, str]:
    """The run's span, padded by an hour on each side to cover bucket edges."""
    start = datetime.fromisoformat(run.started_at).astimezone(UTC) - timedelta(hours=1)
    end = datetime.fromisoformat(run.ended_at).astimezone(UTC) + timedelta(hours=1)
    return start.isoformat(), end.isoformat()


def _fetch(cur, sql: str) -> tuple[list[str], list[tuple[Any, ...]]]:
    cur.execute(sql)
    rows = list(cur.fetchall())
    columns = [column[0] for column in cur.description]
    return columns, rows


def read_metering(conn, run: manifest.RunManifest) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Read `credits_used_compute` per warehouse for this run's window."""
    window_start, window_end = _window(run)
    cur = conn.cursor()
    try:
        return _fetch(
            cur,
            queries.METERING_SQL.format(
                names=_quote_list(run.warehouses),
                window_start=window_start,
                window_end=window_end,
            ),
        )
    finally:
        cur.close()


def read_events(conn, run: manifest.RunManifest) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Read this run's warehouse events, in the order the pairing needs."""
    window_start, window_end = _window(run)
    cur = conn.cursor()
    try:
        return _fetch(
            cur,
            queries.EVENTS_SQL.format(
                names=_quote_list(run.warehouses),
                window_start=window_start,
                window_end=window_end,
            ),
        )
    finally:
        cur.close()


def _polled_seconds(item: manifest.Replicate) -> float:
    start = datetime.fromisoformat(item.resumed_at)
    end = datetime.fromisoformat(item.suspend_issued_at)
    return (end - start).total_seconds()


def _replicate_table(
    run: manifest.RunManifest,
    lifetimes: dict[str, events.Lifetimes],
    billed_seconds: dict[str, float],
) -> ReportTable:
    """What each replicate ran, and what the extra clusters added to its bill.

    The last column is the one the whole experiment is about: the bill, minus
    what the first cluster alone would have cost. Everything the four candidate
    rules disagree about is in that number.
    """
    rows: list[tuple[Any, ...]] = []
    for item in run.replicates:
        life = lifetimes.get(item.warehouse)
        warehouse_s = None if life is None else life.warehouse_seconds
        billed = billed_seconds.get(item.warehouse)
        extras = [] if life is None else life.extra_clusters
        base = None if warehouse_s is None else math.ceil(max(queries.MINIMUM_SECONDS, warehouse_s))
        rows.append(
            (
                item.scenario,
                item.index,
                item.resource_constraint,
                None if warehouse_s is None else round(warehouse_s, 3),
                0 if life is None else len(life.cluster_seconds),
                ""
                if life is None
                else ", ".join(f"{life.cluster_seconds[n]:.1f}" for n in sorted(life.cluster_seconds)),
                ", ".join(f"{start:.1f}" for start, _ in extras),
                None if billed is None else round(billed, 1),
                None if (billed is None or base is None) else round(billed - base, 1),
            )
        )
    return ReportTable(
        step=2,
        title="What each replicate did — warehouse time and cluster time are different quantities",
        columns=[
            "scenario",
            "rep",
            "generation",
            "warehouse_s",
            "clusters",
            "cluster lifetimes",
            "extras started at",
            "billed_s",
            "extras added_s",
        ],
        rows=rows,
    )


def _run_table(run: manifest.RunManifest) -> ReportTable:
    return ReportTable(
        step=1,
        title="Run",
        columns=["run_token", "account", "region", "version", "size", "generation", "started_at", "ended_at"],
        rows=[
            (
                run.run_token,
                run.account,
                run.region,
                run.snowflake_version,
                run.size,
                run.resource_constraint,
                run.started_at,
                run.ended_at,
            )
        ],
    )


def _missing_metering_reason(run: manifest.RunManifest, missing: list[str], moment: datetime) -> str:
    """Explain an empty metering result, and say whether waiting is still the answer.

    The wait is only ever explained here, never enforced above: the rows are
    asked for first, because Snowflake often publishes them long before the
    documented worst case and a time-based gate would hide a finished result.
    """
    names = ", ".join(missing)
    ready_by = manifest.metering_ready_by(run)
    if moment < ready_by:
        return (
            f"no metering rows yet for {names}. A row appears only once its hour has closed, and ACCOUNT_USAGE "
            f"can then lag up to {queries.METERING_LAG_HOURS} hours, so it will be there by "
            f"{ready_by.isoformat()} at the latest — often much sooner. Rerun `report` to check."
        )
    return (
        f"still no metering rows for {names}, and {ready_by.isoformat()} — the latest they were expected — has "
        "passed. Either the view is lagging beyond its documented worst case, or these warehouses never billed "
        "any compute; the step 2 table shows what each replicate did."
    )


def _incomplete_events_reason(items: list[tuple[manifest.Replicate, events.Lifetimes]]) -> str:
    """Name every replicate whose event log cannot be billed against, and why.

    Never a partial verdict: a replicate with a half-read event log has no
    measured lifetime at all, and dropping it quietly would change which
    scenarios the verdict rests on without saying so.
    """
    detail = "; ".join(f"{item.warehouse} ({', '.join(life.missing) or 'no events at all'})" for item, life in items)
    return (
        f"WAREHOUSE_EVENTS_HISTORY is incomplete for {len(items)} replicate(s): {detail}. That view lags up to "
        f"{queries.METERING_LAG_HOURS} hours like the metering one, so rerun `report` later; if the rows never "
        "arrive, those replicates cannot be billed against and the run needs repeating."
    )


def _meter_reason(check: verdict.MeterCheck) -> str:
    """Explain a bill that does not decode to a whole number of seconds.

    Not a small discrepancy to note and move past. The meter charges whole
    seconds, so at the right rate every bill lands on one; a bill that does not
    means the rate used to decode it is wrong, and every number derived from it
    is wrong by the same factor.
    """
    return (
        f"the bills do not decode to whole seconds. At the published rate for a {check.size} warehouse on "
        f"{check.resource_constraint} — {check.published:.3f} credits/hour — {check.worst_warehouse} comes out "
        f"{check.worst_gap_seconds:.3f} seconds away from a whole second, past the "
        f"{queries.WHOLE_SECOND_TOLERANCE} tolerance. The meter bills whole seconds, so this says the rate is "
        "wrong rather than the billing: most likely these warehouses did not run on the generation the manifest "
        "recorded. Nothing further is read, because every scenario's number would be wrong by the same factor."
    )


def _disagreement_table(run: manifest.RunManifest, lifetimes: dict[str, events.Lifetimes]) -> ReportTable | None:
    """Replicates whose event-derived and polled durations do not agree.

    A gap wider than a few seconds means a missed event or a mispaired
    completion marker, not a result — the events are what the verdict uses, so
    the disagreement belongs next to it.
    """
    rows: list[tuple[Any, ...]] = []
    for item in run.replicates:
        life = lifetimes.get(item.warehouse)
        if life is None or life.warehouse_seconds is None:
            continue
        polled = _polled_seconds(item)
        gap = life.warehouse_seconds - polled
        if abs(gap) > queries.POLL_DISAGREEMENT_SECONDS:
            rows.append((item.scenario, item.index, round(life.warehouse_seconds, 3), round(polled, 3), round(gap, 3)))
    if not rows:
        return None
    return ReportTable(
        step=5,
        title="WARNING: event-derived and polled durations disagree — suspect a missed event, not a result",
        columns=["scenario", "rep", "event_warehouse_s", "polled_warehouse_s", "gap_s"],
        rows=rows,
    )


# ACCOUNT_USAGE publishes credits to nine decimal places, so a cluster-second
# figure carries no meaning below a few microseconds. Rounding there discards
# the float noise the credits -> seconds round trip leaves behind, which is
# orders of magnitude smaller than anything the bill itself can express.
BILLED_SECONDS_PRECISION = 6


def _observation(
    scenario: str,
    item: manifest.Replicate,
    life: events.Lifetimes,
    billed: float,
) -> verdict.Observation:
    """One replicate, in the form the arithmetic wants.

    Both times come from the event log rather than the poll clock: the clock says
    when the ALTER was issued, and the log says when the warehouse and each
    cluster actually started and stopped, which is what is billed.
    """
    return verdict.Observation(
        scenario=scenario,
        index=item.index,
        warehouse=item.warehouse,
        warehouse_seconds=life.warehouse_seconds,
        extras=[verdict.Extra(start_offset=start, seconds=seconds) for start, seconds in life.extra_clusters],
        billed_seconds=billed,
    )


def read_report(conn, run: manifest.RunManifest, *, now: datetime | None = None) -> ReportSummary:
    """Assemble the report, and the verdict if every gate before it passes.

    Each gate returns early with a ``not_ready_reason`` and whatever tables were
    built by then: a partial report is still worth reading, and it is what says
    whether to rerun `report` or to rerun the experiment.
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    measured_names = [spec.name for spec in queries.MEASURED_SCENARIOS]
    measured = [item for item in run.replicates if item.scenario in measured_names]

    tables: list[ReportTable] = [_run_table(run), _replicate_table(run, {}, {})]

    metering_columns, metering_rows = read_metering(conn, run)
    tables.append(
        ReportTable(
            step=3,
            title="Billed compute credits per warehouse (cloud services shown but excluded — it lags 6h)",
            columns=metering_columns,
            rows=metering_rows,
        )
    )
    credits = {str(row[0]): float(row[1] or 0.0) for row in metering_rows}
    absent = [item.warehouse for item in measured if item.warehouse not in credits]
    if absent:
        return ReportSummary(tables, None, None, None, _missing_metering_reason(run, absent, moment))

    event_columns, event_rows = read_events(conn, run)
    tables.append(
        ReportTable(
            step=4,
            title="Cluster events — warehouse and cluster lifetimes are read off these, not off the poll clock",
            columns=event_columns,
            rows=event_rows,
        )
    )
    parsed = events.parse_rows(event_columns, event_rows)
    lifetimes = {
        item.warehouse: events.derive(parsed, item.warehouse, expected_clusters=item.target_clusters)
        for item in run.replicates
    }
    tables[1] = _replicate_table(run, lifetimes, {})

    incomplete = [(item, lifetimes[item.warehouse]) for item in measured if not lifetimes[item.warehouse].complete]
    if incomplete:
        return ReportSummary(tables, None, None, None, _incomplete_events_reason(incomplete))

    try:
        published = queries.published_credits_per_hour(run.size, run.resource_constraint)
    except ValueError as error:
        return ReportSummary(
            tables,
            None,
            None,
            None,
            f"the run's warehouses cannot be priced: {error}. Without an hourly rate a bill in credits cannot be "
            "turned into seconds, and seconds are the only thing any of this compares.",
        )

    billed_seconds = {
        warehouse: round(verdict.cluster_seconds(amount, published), BILLED_SECONDS_PRECISION)
        for warehouse, amount in credits.items()
    }
    tables[1] = _replicate_table(run, lifetimes, billed_seconds)

    warning = _disagreement_table(run, lifetimes)
    if warning is not None:
        tables.append(warning)

    observations = [
        _observation(name, item, lifetimes[item.warehouse], billed_seconds[item.warehouse])
        for name in measured_names
        for item in run.for_scenario(name)
    ]
    # The cross-check joins the report, and the verdict ignores it: it is here to
    # confirm the answer outside forced conditions, not to decide it. It is
    # admitted only if its events are complete, since a half-read log has no
    # lifetime to price.
    observations += [
        _observation(queries.NATURAL.name, item, lifetimes[item.warehouse], billed_seconds[item.warehouse])
        for item in run.for_scenario(queries.NATURAL.name)
        if lifetimes[item.warehouse].complete and item.warehouse in billed_seconds
    ]

    meter_check = verdict.check_meter(observations, size=run.size, resource_constraint=run.resource_constraint)
    if not meter_check.ok:
        return ReportSummary(tables, meter_check, None, None, _meter_reason(meter_check))

    minimum_check = verdict.check_minimum([o for o in observations if o.scenario == queries.SHORT.name])
    result = verdict.compute_verdict(observations)

    query_ids = [query_id for item in run.for_scenario(queries.NATURAL.name) for query_id in item.query_ids]
    if query_ids:
        cur = conn.cursor()
        try:
            query_columns, query_rows = _fetch(cur, queries.NATURAL_QUERIES_SQL.format(ids=_quote_list(query_ids)))
        finally:
            cur.close()
        tables.append(
            ReportTable(
                step=6,
                title="The natural scenario's queries — cluster_number should show the second on cluster 2",
                columns=query_columns,
                rows=query_rows,
            )
        )

    return ReportSummary(
        tables=tables,
        meter_check=meter_check,
        minimum_check=minimum_check,
        verdict=result,
        not_ready_reason=None,
    )
