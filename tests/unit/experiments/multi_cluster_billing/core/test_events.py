"""Unit tests for the event-derived warehouse and cluster lifetimes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from experiments.multi_cluster_billing.core import events

WH = "KEEBO_MCB_K2_R1_T"
BASE = datetime(2026, 8, 14, 5, 0, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return BASE + timedelta(seconds=seconds)


def event(name, seconds, cluster=None, state="COMPLETED", warehouse=WH):
    return events.Event(
        warehouse=warehouse, cluster_number=cluster, name=name, reason="TEST", state=state, at=at(seconds)
    )


def cycle(*, extra_cluster_life: float = 15.0, cycle_seconds: float = 90.0) -> list[events.Event]:
    """A k=2 cycle: resume, scale out late, suspend at a fixed offset."""
    scale_out = cycle_seconds - extra_cluster_life
    return [
        event("RESUME_WAREHOUSE", 0.0),
        event("SPINUP_CLUSTER", 0.0, cluster=1),
        event("RESUME_CLUSTER", 0.0, cluster=1),
        event("WAREHOUSE_CONSISTENT", 0.0),
        event("ALTER_WAREHOUSE", scale_out),
        event("SPINUP_CLUSTER", scale_out, cluster=2),
        event("RESUME_CLUSTER", scale_out, cluster=2),
        event("WAREHOUSE_CONSISTENT", scale_out),
        event("SUSPEND_WAREHOUSE", cycle_seconds),
        event("SUSPEND_CLUSTER", cycle_seconds, cluster=1),
        event("SUSPEND_CLUSTER", cycle_seconds, cluster=2),
        event("WAREHOUSE_CONSISTENT", cycle_seconds),
    ]


def test_warehouse_consistent_sorts_after_the_transition_it_completes():
    # The v1 run returned this pair in both orders at different timestamps.
    unordered = [
        event("WAREHOUSE_CONSISTENT", 10.0),
        event("SPINUP_CLUSTER", 10.0, cluster=2),
        event("ALTER_WAREHOUSE", 10.0),
    ]
    assert [e.name for e in events.order_events(unordered)] == [
        "ALTER_WAREHOUSE",
        "SPINUP_CLUSTER",
        "WAREHOUSE_CONSISTENT",
    ]


def test_ordering_is_stable_whatever_the_input_order():
    rows = cycle()
    assert [e.name for e in events.order_events(list(reversed(rows)))] == [e.name for e in rows]


def test_suspend_cluster_rows_sort_by_cluster_number():
    unordered = [
        event("SUSPEND_CLUSTER", 90.0, cluster=2),
        event("WAREHOUSE_CONSISTENT", 90.0),
        event("SUSPEND_CLUSTER", 90.0, cluster=1),
    ]
    ordered = events.order_events(unordered)
    assert [e.cluster_number for e in ordered] == [1, 2, None]


def test_unknown_events_sort_before_the_completion_marker():
    ordered = events.order_events([event("WAREHOUSE_CONSISTENT", 5.0), event("SOME_NEW_EVENT", 5.0)])
    assert [e.name for e in ordered] == ["SOME_NEW_EVENT", "WAREHOUSE_CONSISTENT"]


def test_warehouse_seconds_runs_between_completion_markers():
    result = events.derive(cycle(), WH, expected_clusters=2)
    assert result.warehouse_seconds == pytest.approx(90.0)


def test_every_cluster_gets_its_own_lifetime():
    result = events.derive(cycle(extra_cluster_life=15.0), WH, expected_clusters=2)
    assert result.cluster_seconds == {1: pytest.approx(90.0), 2: pytest.approx(15.0)}
    assert result.cluster_seconds_total == pytest.approx(105.0)
    assert result.extra_cluster_seconds == [pytest.approx(15.0)]
    assert result.complete


def test_a_resize_in_the_middle_does_not_end_the_warehouse_interval():
    # The ALTER's own WAREHOUSE_CONSISTENT must not be mistaken for the suspend's.
    result = events.derive(cycle(), WH, expected_clusters=2)
    assert result.warehouse_seconds == pytest.approx(90.0)


def test_five_clusters_are_all_recovered():
    rows = [
        event("RESUME_WAREHOUSE", 0.0),
        event("RESUME_CLUSTER", 0.0, cluster=1),
        event("WAREHOUSE_CONSISTENT", 0.0),
        event("ALTER_WAREHOUSE", 70.0),
    ]
    for n in range(2, 6):
        rows.append(event("RESUME_CLUSTER", 70.0 + n * 0.5, cluster=n))
    rows.append(event("WAREHOUSE_CONSISTENT", 73.0))
    rows.append(event("SUSPEND_WAREHOUSE", 90.0))
    for n in range(1, 6):
        rows.append(event("SUSPEND_CLUSTER", 90.0, cluster=n))
    rows.append(event("WAREHOUSE_CONSISTENT", 90.0))

    result = events.derive(rows, WH, expected_clusters=5)
    assert result.complete
    assert sorted(result.cluster_seconds) == [1, 2, 3, 4, 5]
    # Each extra cluster resumed 0.5 s after the one before, and all five were
    # suspended together at 90 s.
    assert result.extra_cluster_seconds == [
        pytest.approx(19.0),
        pytest.approx(18.5),
        pytest.approx(18.0),
        pytest.approx(17.5),
    ]


def test_a_truncated_event_set_is_reported_not_guessed():
    rows = [e for e in cycle() if not (e.name == "SUSPEND_CLUSTER" and e.cluster_number == 2)]
    result = events.derive(rows, WH, expected_clusters=2)
    assert not result.complete
    assert any("cluster 2" in complaint for complaint in result.missing)
    assert 2 not in result.cluster_seconds


def test_a_missing_suspend_leaves_warehouse_seconds_unknown():
    rows = [e for e in cycle() if e.name != "SUSPEND_WAREHOUSE"]
    result = events.derive(rows, WH, expected_clusters=2)
    assert result.warehouse_seconds is None
    assert not result.complete
    assert any("SUSPEND_WAREHOUSE" in complaint for complaint in result.missing)


def test_other_warehouses_are_ignored():
    rows = [*cycle(), event("SUSPEND_CLUSTER", 5.0, cluster=1, warehouse="SOMETHING_ELSE")]
    result = events.derive(rows, WH, expected_clusters=2)
    assert result.cluster_seconds[1] == pytest.approx(90.0)


def test_parse_rows_maps_columns_by_name():
    columns = ["WAREHOUSE_NAME", "CLUSTER_NUMBER", "EVENT_NAME", "EVENT_REASON", "EVENT_STATE", "TIMESTAMP"]
    rows = [(WH, 1, "RESUME_CLUSTER", "MIN_CLUSTER_COUNT", "COMPLETED", at(0.0))]
    parsed = events.parse_rows(columns, rows)
    assert parsed == [
        events.Event(
            warehouse=WH,
            cluster_number=1,
            name="RESUME_CLUSTER",
            reason="MIN_CLUSTER_COUNT",
            state="COMPLETED",
            at=at(0.0),
        )
    ]


def test_parse_rows_survives_a_missing_event_state_column():
    # event_state is projected and printed, never branched on; an older account
    # that does not expose it must still produce lifetimes.
    columns = ["WAREHOUSE_NAME", "CLUSTER_NUMBER", "EVENT_NAME", "EVENT_REASON", "TIMESTAMP"]
    rows = [(WH, 1, "RESUME_CLUSTER", "MIN_CLUSTER_COUNT", at(0.0))]
    assert events.parse_rows(columns, rows)[0].state is None
