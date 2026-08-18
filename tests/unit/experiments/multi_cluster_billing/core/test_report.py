"""Unit tests for the report assembly."""

from datetime import UTC, datetime, timedelta

import pytest

from experiments.multi_cluster_billing.core import manifest, queries, report, verdict
from experiments.multi_cluster_billing.core.questions import OWN_MINUTE, PER_SECOND, SHARES_THE_MINUTE

BASE = datetime(2026, 8, 14, 5, 0, 0, tzinfo=UTC)
EVENT_COLUMNS = ["WAREHOUSE_NAME", "CLUSTER_NUMBER", "EVENT_NAME", "EVENT_REASON", "EVENT_STATE", "TIMESTAMP"]
METERING_COLUMNS = ["WAREHOUSE_NAME", "COMPUTE_CREDITS", "CLOUD_SERVICES_CREDITS", "FIRST_HOUR", "LAST_HOUR", "ROWS"]

RATE = 1.35
GEN1_RATE = 1.0

# Every measured scenario, plus the cross-check given a shape of its own: the
# forced ones are pinned by the spec table, and `natural` has no fixed shape
# because nothing forces its second cluster up at a chosen moment.
SPECS = {spec.name: spec for spec in queries.MEASURED_SCENARIOS}
NATURAL_SHAPE = (95.0, 12.0, 40.0)  # warehouse seconds, when the extra started, how long it lived


def shape_of(scenario: str) -> tuple[float, float, float, int]:
    """(warehouse seconds, extra start offset, extra lifetime, extra cluster count)."""
    if scenario == queries.NATURAL.name:
        warehouse_s, start, life = NATURAL_SHAPE
        return warehouse_s, start, life, 1
    spec = SPECS[scenario]
    return (
        float(spec.cycle_seconds),
        float(spec.scale_out_at_seconds),
        float(spec.extra_cluster_seconds),
        spec.target_clusters - 1,
    )


def extras_of(scenario: str) -> list[verdict.Extra]:
    _, start, life, count = shape_of(scenario)
    return [verdict.Extra(start_offset=start, seconds=life) for _ in range(count)]


def warehouse_of(scenario, replicate):
    return f"KEEBO_MCB_{scenario.upper()}_R{replicate}_T"


def replicate_of(scenario, replicate, offset):
    warehouse_s, start_offset, _, count = shape_of(scenario)
    start = BASE + timedelta(seconds=offset)
    return manifest.Replicate(
        scenario=scenario,
        index=replicate,
        warehouse=warehouse_of(scenario, replicate),
        size="XSMALL",
        resource_constraint="STANDARD_GEN_2",
        target_clusters=1 + count,
        cycle_seconds=int(warehouse_s),
        kind="forced" if scenario != queries.NATURAL.name else "natural",
        resumed_at=start.isoformat(),
        resume_confirmed_at=(start + timedelta(seconds=3)).isoformat(),
        scaled_at=(start + timedelta(seconds=start_offset)).isoformat(),
        target_seen_at=(start + timedelta(seconds=start_offset + 2)).isoformat(),
        suspend_issued_at=(start + timedelta(seconds=warehouse_s)).isoformat(),
        suspend_confirmed_at=(start + timedelta(seconds=warehouse_s + 3)).isoformat(),
        max_started_clusters=1 + count,
        query_ids=[],
        polls=[],
        error=None,
    )


def event_rows_for(scenario, replicate, offset):
    """The event log a replicate of this shape would leave behind."""
    warehouse_s, start_offset, _, count = shape_of(scenario)
    name = warehouse_of(scenario, replicate)
    start = BASE + timedelta(seconds=offset)

    def row(event, seconds, cluster=None):
        return (name, cluster, event, "TEST", "COMPLETED", start + timedelta(seconds=seconds))

    rows = [row("RESUME_WAREHOUSE", 0.0), row("RESUME_CLUSTER", 0.0, 1), row("WAREHOUSE_CONSISTENT", 0.0)]
    if count:
        rows.append(row("ALTER_WAREHOUSE", start_offset))
        for n in range(2, 2 + count):
            rows.append(row("RESUME_CLUSTER", start_offset, n))
        rows.append(row("WAREHOUSE_CONSISTENT", start_offset + 2))
    rows.append(row("SUSPEND_WAREHOUSE", warehouse_s))
    for n in range(1, 2 + count):
        rows.append(row("SUSPEND_CLUSTER", warehouse_s, n))
    rows.append(row("WAREHOUSE_CONSISTENT", warehouse_s))
    return rows


def metering_row(scenario, replicate, rule=OWN_MINUTE, rate=RATE):
    warehouse_s, _, _, _ = shape_of(scenario)
    seconds = verdict.predict(rule, warehouse_seconds=warehouse_s, extras=extras_of(scenario))
    return (warehouse_of(scenario, replicate), seconds * rate / 3600.0, 0.001, BASE, BASE, 1)


def build(replicates=4, rule=OWN_MINUTE, rate=RATE, scenarios=None):
    items, events_rows, metering_rows = [], [], []
    offset = 0
    for scenario in scenarios or list(SPECS):
        for r in range(1, replicates + 1):
            items.append(replicate_of(scenario, r, offset))
            events_rows.extend(event_rows_for(scenario, r, offset))
            metering_rows.append(metering_row(scenario, r, rule, rate))
            offset += 400
    run = manifest.RunManifest(
        schema_version=manifest.SCHEMA_VERSION,
        run_token="T",
        account="ACC",
        region="R",
        snowflake_version="9",
        size="XSMALL",
        resource_constraint="STANDARD_GEN_2",
        started_at=BASE.isoformat(),
        ended_at=(BASE + timedelta(seconds=offset)).isoformat(),
        replicates=items,
    )
    return run, events_rows, metering_rows


def add_natural(run, events_rows, metering_rows, *, rule=OWN_MINUTE, offset=90_000):
    item = replicate_of(queries.NATURAL.name, 1, offset)
    run.replicates.append(item)
    events_rows.extend(event_rows_for(queries.NATURAL.name, 1, offset))
    metering_rows.append(metering_row(queries.NATURAL.name, 1, rule))
    return run, events_rows, metering_rows


class ScriptedCursor:
    """Answers the metering query, then the events query, then anything else."""

    def __init__(self, metering_rows, events_rows):
        self._metering = (METERING_COLUMNS, metering_rows)
        self._events = (EVENT_COLUMNS, events_rows)
        self.description = [("col",)]
        self._rows = []

    def execute(self, sql):
        if "WAREHOUSE_METERING_HISTORY" in sql:
            columns, self._rows = self._metering
        elif "WAREHOUSE_EVENTS_HISTORY" in sql:
            columns, self._rows = self._events
        else:
            columns, self._rows = ["col"], []
        self.description = [(c,) for c in columns]
        return self

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class ScriptedConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def summarise(run, events_rows, metering_rows):
    conn = ScriptedConnection(ScriptedCursor(metering_rows, events_rows))
    return report.read_report(conn, run, now=BASE + timedelta(hours=30))


def row_value(table, row, column):
    return row[table.columns.index(column)]


def test_a_clean_run_reaches_a_verdict():
    summary = summarise(*build())
    assert summary.not_ready_reason is None
    assert summary.verdict.outcome == OWN_MINUTE


def test_a_run_billed_by_the_second_is_told_apart_from_one_billed_by_the_minute():
    assert summarise(*build(rule=PER_SECOND)).verdict.outcome == PER_SECOND
    assert summarise(*build(rule=SHARES_THE_MINUTE)).verdict.outcome == SHARES_THE_MINUTE


def test_the_rate_is_the_published_one_not_one_measured_from_control():
    # v2 divided control's bill by control's runtime and got a rate half a
    # percent high, because the meter had rounded the bill up to a whole second.
    summary = summarise(*build())
    assert summary.meter_check.published == pytest.approx(RATE)
    assert summary.meter_check.worst_gap_seconds == pytest.approx(0.0, abs=1e-6)
    assert summary.meter_check.ok


def test_bills_that_do_not_decode_to_whole_seconds_stop_the_report():
    # A Gen1 bill read as Gen2: every figure 26% low, and none of them landing
    # on a whole second. No verdict is reached from numbers in that state.
    summary = summarise(*build(rate=GEN1_RATE))
    assert summary.verdict is None
    assert not summary.meter_check.ok
    assert "whole seconds" in summary.not_ready_reason
    assert "1.350" in summary.not_ready_reason


def test_a_size_with_no_published_rate_stops_the_report():
    run, events_rows, metering_rows = build()
    run.size = "GIGANTIC"
    summary = summarise(run, events_rows, metering_rows)
    assert summary.verdict is None
    assert "cannot be priced" in summary.not_ready_reason


def test_the_replicate_table_separates_warehouse_time_from_what_the_extras_added():
    summary = summarise(*build())
    table = next(t for t in summary.tables if "warehouse_s" in t.columns)
    for column in ("cluster lifetimes", "extras started at", "billed_s", "extras added_s"):
        assert column in table.columns
    k5 = next(row for row in table.rows if row[0] == queries.K5.name)
    assert row_value(table, k5, "warehouse_s") == pytest.approx(float(queries.K5.cycle_seconds))
    assert row_value(table, k5, "clusters") == queries.K5.target_clusters
    # Four extra clusters at a minute each, on top of the 90 the warehouse ran.
    assert row_value(table, k5, "extras added_s") == pytest.approx(240.0)


def test_the_replicate_table_records_when_each_extra_cluster_started():
    summary = summarise(*build())
    table = next(t for t in summary.tables if "warehouse_s" in t.columns)
    inside = next(row for row in table.rows if row[0] == queries.INSIDE.name)
    assert row_value(table, inside, "extras started at") == f"{float(queries.INSIDE.scale_out_at_seconds):.1f}"


def test_the_minimum_premise_is_checked_and_reported():
    summary = summarise(*build())
    assert summary.minimum_check.holds
    assert summary.minimum_check.mean_billed == pytest.approx(60.0, abs=0.5)
    assert summary.minimum_check.mean_warehouse_seconds == pytest.approx(float(queries.SHORT.cycle_seconds), abs=0.5)


def test_missing_metering_names_the_warehouses_and_says_when_to_retry():
    run, events_rows, metering_rows = build()
    summary = summarise(run, events_rows, metering_rows[:-2])
    assert summary.verdict is None
    assert queries.K5.name.upper() in summary.not_ready_reason
    assert "metering" in summary.not_ready_reason.lower()


def test_metering_still_inside_its_lag_window_says_to_wait_rather_than_to_rerun():
    run, events_rows, metering_rows = build()
    conn = ScriptedConnection(ScriptedCursor(metering_rows[:-2], events_rows))
    summary = report.read_report(conn, run, now=BASE + timedelta(minutes=1))
    assert summary.verdict is None
    assert "Rerun `report`" in summary.not_ready_reason


def test_missing_events_never_yield_a_verdict_on_partial_data():
    run, events_rows, metering_rows = build()
    truncated = [r for r in events_rows if r[2] != "SUSPEND_CLUSTER" or r[1] != 5]
    summary = summarise(run, truncated, metering_rows)
    assert summary.verdict is None
    assert "cluster 5" in summary.not_ready_reason


def test_the_report_never_blocks_waiting():
    run, events_rows, metering_rows = build()
    conn = ScriptedConnection(ScriptedCursor([], []))
    summary = report.read_report(conn, run, now=BASE + timedelta(minutes=1))
    assert summary.verdict is None
    assert summary.tables  # the run and replicate tables are printed regardless


def test_the_natural_scenario_is_reported_but_does_not_decide():
    summary = summarise(*add_natural(*build()))
    assert summary.verdict is not None
    names = [s.name for s in summary.verdict.scenarios]
    assert queries.NATURAL.name in names
    assert all(s.name != queries.NATURAL.name for s in verdict.deciding(summary.verdict.scenarios))


def test_a_cross_check_billed_differently_does_not_overturn_the_verdict():
    summary = summarise(*add_natural(*build(), rule=PER_SECOND))
    assert summary.verdict.outcome == OWN_MINUTE
    natural = next(s for s in summary.verdict.scenarios if s.name == queries.NATURAL.name)
    assert OWN_MINUTE not in natural.fits


def test_a_cross_check_with_a_half_read_event_log_is_dropped_rather_than_priced():
    run, events_rows, metering_rows = add_natural(*build())
    dropped = warehouse_of(queries.NATURAL.name, 1)
    truncated = [r for r in events_rows if not (r[0] == dropped and r[2] == "SUSPEND_CLUSTER")]
    summary = summarise(run, truncated, metering_rows)
    assert summary.not_ready_reason is None
    assert summary.verdict.outcome == OWN_MINUTE
    assert all(s.name != queries.NATURAL.name for s in summary.verdict.scenarios)


def test_a_replicate_that_never_reached_its_clusters_is_excluded_and_named():
    run, events_rows, metering_rows = build()
    dropped = warehouse_of(queries.K5.name, 2)
    truncated = [r for r in events_rows if r[0] != dropped]
    summary = summarise(run, truncated, metering_rows)
    assert summary.verdict is None
    assert dropped in summary.not_ready_reason


def test_a_disagreement_between_the_event_log_and_the_poll_clock_is_flagged():
    run, events_rows, metering_rows = build()
    run.replicates[0].suspend_issued_at = (
        datetime.fromisoformat(run.replicates[0].resumed_at) + timedelta(seconds=600)
    ).isoformat()
    summary = summarise(run, events_rows, metering_rows)
    warning = next(t for t in summary.tables if "disagree" in t.title)
    assert warning.rows
