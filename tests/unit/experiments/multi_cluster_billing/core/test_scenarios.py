"""Unit tests for the run engine.

Time is injected: `clock` returns a controllable UTC instant and `sleep` records
what it was asked to wait for instead of waiting, so a 75-second idle costs
nothing in the test suite.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from experiments.multi_cluster_billing.core import queries, scenarios

SHOW_COLUMNS = [("name",), ("state",), ("started_clusters",), ("queued",), ("size",)]

#: The wider result the settings readback needs: the two pinned properties too.
SHOW_DESCRIPTION = [
    ("name",),
    ("state",),
    ("started_clusters",),
    ("queued",),
    ("enable_query_acceleration",),
    ("resource_constraint",),
]


def show_row(state="STARTED", clusters=1, queued=0, qas=False, constraint="STANDARD_GEN_2"):
    return ([("W", state, clusters, queued, qas, constraint)], SHOW_DESCRIPTION)


class FakeClock:
    """A clock the sleeps drive, so a 90-second cycle costs no wall time."""

    def __init__(self) -> None:
        self.now = datetime(2026, 8, 14, 5, 0, 0, tzinfo=UTC)
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)

    def drift(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def make_clock(start: datetime | None = None):
    """A clock that advances only when `sleep` is called, plus the sleeps taken."""
    now = [start or datetime(2026, 8, 13, 14, 0, tzinfo=UTC)]
    slept: list[float] = []

    def clock() -> datetime:
        return now[0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] = now[0] + timedelta(seconds=seconds)

    return clock, sleep, slept


def show_response(state: str, started_clusters: int, queued: int = 0):
    return ([("W", state, started_clusters, queued, "X-Small")], SHOW_COLUMNS)


# A slot for a statement whose result nobody reads, such as an ALTER WAREHOUSE.
STATEMENT_OK = ([("done",)], [("status",)])


def test_show_warehouse_maps_columns_by_name(make_cursor, make_connection):
    cursor = make_cursor(responses=[show_response("STARTED", 2, 1)])
    make_connection(cursor)

    state = scenarios.show_warehouse(cursor, "W")

    # The settings columns are absent from this result, and absent is not wrong.
    assert state == scenarios.WarehouseState(
        state="STARTED",
        started_clusters=2,
        queued=1,
        enable_query_acceleration=None,
        resource_constraint=None,
    )
    assert cursor.executed == ["SHOW WAREHOUSES LIKE 'W'"]


def test_show_warehouse_raises_when_the_warehouse_is_missing(make_cursor):
    cursor = make_cursor(responses=[([], SHOW_COLUMNS)])

    try:
        scenarios.show_warehouse(cursor, "W")
    except ValueError as exc:
        assert "W" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_wait_for_returns_as_soon_as_the_predicate_holds(make_cursor):
    cursor = make_cursor(
        responses=[
            show_response("STARTING", 0),
            show_response("STARTED", 1),
            show_response("STARTED", 2),
        ]
    )
    clock, sleep, slept = make_clock()
    polls: list = []

    state = scenarios.wait_for(
        cursor,
        "W",
        lambda s: s.started_clusters >= 2,
        timeout=60,
        interval=1,
        clock=clock,
        sleep=sleep,
        polls=polls,
    )

    assert state.started_clusters == 2
    assert len(polls) == 3
    assert polls[-1].started_clusters == 2
    assert slept == [1, 1]  # no sleep before the first poll, none after the match


def test_wait_for_returns_none_on_timeout(make_cursor):
    cursor = make_cursor(fetch=[("W", "STARTED", 1, 0, "X-Small")], description=SHOW_COLUMNS)
    clock, sleep, _ = make_clock()
    polls: list = []

    state = scenarios.wait_for(
        cursor,
        "W",
        lambda s: s.started_clusters >= 2,
        timeout=5,
        interval=1,
        clock=clock,
        sleep=sleep,
        polls=polls,
    )

    assert state is None
    assert len(polls) >= 5


# --------------------------------------------------------------------------- #
# The settings readback
# --------------------------------------------------------------------------- #
def test_show_warehouse_reads_the_settings_back(make_cursor):
    cur = make_cursor(responses=[show_row(qas=True, constraint="STANDARD_GEN_1")])
    state = scenarios.show_warehouse(cur, "W")
    assert state.enable_query_acceleration is True
    assert state.resource_constraint == "STANDARD_GEN_1"


def test_show_warehouse_reads_a_string_valued_flag(make_cursor):
    # SHOW WAREHOUSES has returned 'false' as text on some accounts.
    cur = make_cursor(responses=[([("W", "STARTED", 1, 0, "false", "STANDARD_GEN_2")], SHOW_DESCRIPTION)])
    assert scenarios.show_warehouse(cur, "W").enable_query_acceleration is False


def test_show_warehouse_accepts_a_generation_column_instead(make_cursor):
    # An account that takes GENERATION rather than RESOURCE_CONSTRAINT may report
    # it back the same way. Either column answers the same question.
    description = [("name",), ("state",), ("started_clusters",), ("queued",), ("generation",)]
    cur = make_cursor(responses=[([("W", "STARTED", 1, 0, "2")], description)])
    assert scenarios.show_warehouse(cur, "W").resource_constraint == "STANDARD_GEN_2"


def test_show_warehouse_ignores_a_generation_it_cannot_map(make_cursor):
    description = [("name",), ("state",), ("started_clusters",), ("queued",), ("generation",)]
    cur = make_cursor(responses=[([("W", "STARTED", 1, 0, "9")], description)])
    assert scenarios.show_warehouse(cur, "W").resource_constraint is None


def test_query_acceleration_left_on_aborts_the_run():
    state = scenarios.WarehouseState("STARTED", 1, 0, True, "STANDARD_GEN_2")
    with pytest.raises(ValueError, match="ENABLE_QUERY_ACCELERATION"):
        scenarios.assert_settings(state, warehouse="W", resource_constraint="STANDARD_GEN_2")


def test_the_wrong_generation_aborts_the_run():
    # This is the v1 failure: a Gen2 warehouse decoded at the Gen1 rate.
    state = scenarios.WarehouseState("STARTED", 1, 0, False, "STANDARD_GEN_1")
    with pytest.raises(ValueError, match="STANDARD_GEN_1"):
        scenarios.assert_settings(state, warehouse="W", resource_constraint="STANDARD_GEN_2")


def test_settings_that_match_are_accepted():
    state = scenarios.WarehouseState("STARTED", 1, 0, False, "STANDARD_GEN_2")
    assert scenarios.assert_settings(state, warehouse="W", resource_constraint="STANDARD_GEN_2") is None


def test_an_unreported_setting_is_accepted_rather_than_guessed():
    # An account that does not expose the column must not fail the run.
    state = scenarios.WarehouseState("STARTED", 1, 0, None, None)
    assert scenarios.assert_settings(state, warehouse="W", resource_constraint="STANDARD_GEN_2") is None


# --------------------------------------------------------------------------- #
# The fixed-offset cycle
# --------------------------------------------------------------------------- #
def test_sleep_until_waits_only_the_remaining_time():
    clock = FakeClock()
    anchor = clock()
    clock.drift(12.0)  # a slow resume
    scenarios.sleep_until(anchor, 70.0, clock=clock, sleeper=clock.sleep, echo=lambda _m: None, label="scale-out")
    assert clock.slept == [pytest.approx(58.0)]
    assert (clock() - anchor).total_seconds() == pytest.approx(70.0)


def test_sleep_until_does_not_sleep_backwards():
    clock = FakeClock()
    anchor = clock()
    clock.drift(95.0)
    messages: list[str] = []
    scenarios.sleep_until(anchor, 70.0, clock=clock, sleeper=clock.sleep, echo=messages.append, label="scale-out")
    assert clock.slept == []
    assert any("scale-out" in m and "late" in m for m in messages)


def test_the_cycle_suspends_at_a_fixed_offset_whatever_provisioning_did(make_cursor):
    clock = FakeClock()
    cur = make_cursor(responses=[show_row() for _ in range(40)])
    record = scenarios.run_cycle(
        cur,
        warehouse="KEEBO_MCB_BRIEF_R1_T",
        spec=queries.BRIEF,
        replicate=1,
        size="XSMALL",
        resource_constraint="STANDARD_GEN_2",
        clock=clock,
        sleep=clock.sleep,
    )
    elapsed = (
        datetime.fromisoformat(record.suspend_issued_at) - datetime.fromisoformat(record.resumed_at)
    ).total_seconds()
    assert elapsed == pytest.approx(90.0)


def test_the_scale_out_lands_twenty_seconds_before_the_suspend(make_cursor):
    clock = FakeClock()
    cur = make_cursor(responses=[show_row() for _ in range(40)])
    record = scenarios.run_cycle(
        cur,
        warehouse="W_BRIEF",
        spec=queries.BRIEF,
        replicate=1,
        size="XSMALL",
        resource_constraint="STANDARD_GEN_2",
        clock=clock,
        sleep=clock.sleep,
    )
    scaled = (datetime.fromisoformat(record.scaled_at) - datetime.fromisoformat(record.resumed_at)).total_seconds()
    assert scaled == pytest.approx(70.0)


def test_single_cluster_scenarios_issue_the_same_no_op_statement(make_cursor):
    # Every scenario executes the same statement sequence; only the value differs.
    clock = FakeClock()
    cur = make_cursor(responses=[show_row() for _ in range(40)])
    scenarios.run_cycle(
        cur,
        warehouse="W_CONTROL",
        spec=queries.CONTROL,
        replicate=1,
        size="XSMALL",
        resource_constraint="STANDARD_GEN_2",
        clock=clock,
        sleep=clock.sleep,
    )
    assert "ALTER WAREHOUSE W_CONTROL SET MIN_CLUSTER_COUNT = 1" in cur.executed


def test_suspend_is_issued_before_min_cluster_count_is_reset(make_cursor):
    # Resetting first would make the extra clusters drainable while the warehouse
    # is still running, handing their lifetimes to the scale-in gate.
    clock = FakeClock()
    cur = make_cursor(responses=[show_row() for _ in range(40)])
    scenarios.run_cycle(
        cur,
        warehouse="W_K5",
        spec=queries.K5,
        replicate=1,
        size="XSMALL",
        resource_constraint="STANDARD_GEN_2",
        clock=clock,
        sleep=clock.sleep,
    )
    suspend = cur.executed.index("ALTER WAREHOUSE W_K5 SUSPEND")
    reset = len(cur.executed) - 1 - cur.executed[::-1].index("ALTER WAREHOUSE W_K5 SET MIN_CLUSTER_COUNT = 1")
    assert suspend < reset


def test_the_short_cycle_keeps_the_same_shape_at_its_own_length(make_cursor):
    clock = FakeClock()
    cur = make_cursor(responses=[show_row() for _ in range(40)])
    record = scenarios.run_cycle(
        cur,
        warehouse="W_SHORT",
        spec=queries.SHORT,
        replicate=1,
        size="XSMALL",
        resource_constraint="STANDARD_GEN_2",
        clock=clock,
        sleep=clock.sleep,
    )
    elapsed = (
        datetime.fromisoformat(record.suspend_issued_at) - datetime.fromisoformat(record.resumed_at)
    ).total_seconds()
    scaled = (datetime.fromisoformat(record.scaled_at) - datetime.fromisoformat(record.resumed_at)).total_seconds()
    assert elapsed == pytest.approx(45.0)
    assert scaled == pytest.approx(25.0)


def test_the_replicate_records_its_scenario_and_settings(make_cursor):
    clock = FakeClock()
    cur = make_cursor(responses=[show_row() for _ in range(40)])
    record = scenarios.run_cycle(
        cur,
        warehouse="W_K5",
        spec=queries.K5,
        replicate=3,
        size="XSMALL",
        resource_constraint="STANDARD_GEN_2",
        clock=clock,
        sleep=clock.sleep,
    )
    assert (record.scenario, record.index, record.target_clusters, record.cycle_seconds) == ("k5", 3, 5, 90)
    assert record.resource_constraint == "STANDARD_GEN_2"
    assert record.error is None


def test_a_cluster_that_never_starts_is_recorded_not_raised(make_cursor):
    clock = FakeClock()
    # Never reports more than one cluster, so brief times out waiting for its second.
    cur = make_cursor(responses=[show_row(clusters=1) for _ in range(600)])
    record = scenarios.run_cycle(
        cur,
        warehouse="W_BRIEF",
        spec=queries.BRIEF,
        replicate=1,
        size="XSMALL",
        resource_constraint="STANDARD_GEN_2",
        poll_timeout=5.0,
        clock=clock,
        sleep=clock.sleep,
    )
    assert record.target_seen_at is None
    assert record.max_started_clusters == 1
    assert "ALTER WAREHOUSE W_BRIEF SUSPEND" in cur.executed


# --------------------------------------------------------------------------- #
# The natural scenario
# --------------------------------------------------------------------------- #
def _natural_rep_cursor(make_cursor):
    """A cursor scripted through one successful natural rep.

    The async submissions consume slots too: `execute_async` advances the same
    queue as `execute`, because both cursors here are the one fake.
    """
    return make_cursor(
        responses=[
            STATEMENT_OK,  # ALTER ... RESUME
            show_response("STARTED", 1),  # the poll confirming the resume
            STATEMENT_OK,  # USE WAREHOUSE W
            STATEMENT_OK,  # the first async query
            STATEMENT_OK,  # the second async query, which queues
            show_response("STARTED", 1),  # polled while query 2 is still queued
            show_response("STARTED", 2),  # Snowflake started cluster 2 by itself
            STATEMENT_OK,  # ALTER ... SUSPEND
            show_response("SUSPENDED", 0),  # the poll confirming the suspend
        ],
        fetch=[("W", "SUSPENDED", 0, 0, "X-Small")],
        description=SHOW_COLUMNS,
    )


def test_natural_rep_submits_two_concurrent_queries_and_records_their_ids(make_cursor, make_connection):
    cursor = _natural_rep_cursor(make_cursor)
    conn = make_connection(cursor)
    clock, sleep, _ = make_clock()

    rep = scenarios.run_natural_rep(
        conn,
        cursor,
        warehouse="W",
        spec=queries.NATURAL,
        replicate=1,
        size="XSMALL",
        resource_constraint="STANDARD_GEN_2",
        rowcount=1000,
        idle_seconds=75,
        clock=clock,
        sleep=sleep,
    )

    assert len(rep.query_ids) == 2
    assert len(set(rep.query_ids)) == 2
    assert sum("GENERATOR(ROWCOUNT => 1000)" in s for s in cursor.executed) == 2
    assert "USE WAREHOUSE W" in cursor.executed
    assert rep.scenario == "natural"
    assert rep.cycle_seconds == 0
    assert rep.target_seen_at is not None
    assert rep.scaled_at is None  # nothing forced the scale-out


def test_natural_rep_waits_for_both_queries_before_suspending(make_cursor, make_connection):
    cursor = _natural_rep_cursor(make_cursor)
    conn = make_connection(cursor)
    clock, sleep, _ = make_clock()

    # Query 1 finishes immediately; query 2 reports running twice first.
    conn.query_states["fake-query-1"] = [False]
    conn.query_states["fake-query-2"] = [True, True, False]

    scenarios.run_natural_rep(
        conn,
        cursor,
        warehouse="W",
        spec=queries.NATURAL,
        replicate=1,
        size="XSMALL",
        resource_constraint="STANDARD_GEN_2",
        rowcount=1000,
        idle_seconds=75,
        clock=clock,
        sleep=sleep,
    )

    suspend_at = cursor.executed.index("ALTER WAREHOUSE W SUSPEND")
    last_query_at = max(i for i, s in enumerate(cursor.executed) if "GENERATOR" in s)
    assert last_query_at < suspend_at
    assert conn.query_states["fake-query-2"] == []  # drained, so we really waited


def test_natural_rep_restores_the_session_warehouse(make_cursor, make_connection):
    cursor = _natural_rep_cursor(make_cursor)
    conn = make_connection(cursor)
    clock, sleep, _ = make_clock()

    scenarios.run_natural_rep(
        conn,
        cursor,
        warehouse="W",
        spec=queries.NATURAL,
        replicate=1,
        size="XSMALL",
        resource_constraint="STANDARD_GEN_2",
        rowcount=1000,
        idle_seconds=75,
        previous_warehouse="PREVIOUS_WH",
        clock=clock,
        sleep=sleep,
    )

    assert cursor.executed[-1] == "USE WAREHOUSE PREVIOUS_WH"


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def test_the_schedule_is_a_latin_square():
    width = len(queries.MEASURED_SCENARIOS)
    plan = scenarios.schedule(queries.MEASURED_SCENARIOS, 4)
    assert len(plan) == width * 4
    positions: dict[str, list[int]] = {}
    for i, (spec, _replicate) in enumerate(plan):
        positions.setdefault(spec.name, []).append(i % width)
    # A scenario never repeats a position within the run, so none of them is
    # systematically early or late — which matters because a run takes an hour
    # and the account it runs on is not otherwise idle.
    for name, seen in positions.items():
        assert len(set(seen)) == len(seen) == 4, name
    # And every position is used the same number of times.
    counts = [sum(1 for seen in positions.values() if slot in seen) for slot in range(width)]
    assert len(set(counts)) == 1


def test_the_schedule_numbers_replicates_within_each_scenario():
    plan = scenarios.schedule(queries.MEASURED_SCENARIOS, 4)
    for spec in queries.MEASURED_SCENARIOS:
        indices = [replicate for s, replicate in plan if s.name == spec.name]
        assert sorted(indices) == [1, 2, 3, 4]


def test_the_schedule_scales_to_other_replicate_counts():
    width = len(queries.MEASURED_SCENARIOS)
    assert len(scenarios.schedule(queries.MEASURED_SCENARIOS, 1)) == width
    assert len(scenarios.schedule(queries.MEASURED_SCENARIOS, 2)) == 2 * width


def test_the_schedule_rejects_a_nonsense_replicate_count():
    with pytest.raises(ValueError, match="replicates"):
        scenarios.schedule(queries.MEASURED_SCENARIOS, 0)


def test_resource_monitors_are_reported_so_a_suspend_is_not_a_surprise(make_cursor):
    cur = make_cursor(responses=[([("MONITOR_A",), ("MONITOR_B",)], [("name",)])])
    assert scenarios.check_resource_monitors(cur) == ["MONITOR_A", "MONITOR_B"]


def test_a_missing_resource_monitor_privilege_is_not_fatal(make_cursor):
    class Boom:
        description = [("name",)]

        def execute(self, sql):
            raise RuntimeError("insufficient privileges")

        def fetchall(self):
            return []

    assert scenarios.check_resource_monitors(Boom()) == []


def test_the_manifest_is_checkpointed_after_every_warehouse(make_cursor, make_connection):
    clock = FakeClock()
    cur = make_cursor(responses=[show_row() for _ in range(2000)])
    conn = make_connection(cur)
    seen: list[int] = []

    scenarios.run_experiment(
        conn,
        replicates=1,
        include_natural=False,
        clock=clock,
        sleep=clock.sleep,
        checkpoint=lambda record: seen.append(len(record.replicates)),
    )
    # One write per warehouse — a crash 20 minutes in must still leave `cleanup`
    # something to work from — and a last one that stamps `ended_at`.
    width = len(queries.MEASURED_SCENARIOS)
    assert seen == [*range(1, width + 1), width]


def test_the_run_records_the_generation_it_pinned(make_cursor, make_connection):
    clock = FakeClock()
    cur = make_cursor(responses=[show_row() for _ in range(2000)])
    record = scenarios.run_experiment(
        make_connection(cur), replicates=1, include_natural=False, clock=clock, sleep=clock.sleep
    )
    assert record.resource_constraint == "STANDARD_GEN_2"
    assert all(r.resource_constraint == "STANDARD_GEN_2" for r in record.replicates)
    assert record.ended_at is not None


def test_every_warehouse_name_is_unique(make_cursor, make_connection):
    clock = FakeClock()
    cur = make_cursor(responses=[show_row() for _ in range(2000)])
    record = scenarios.run_experiment(
        make_connection(cur), replicates=2, include_natural=False, clock=clock, sleep=clock.sleep
    )
    assert len(set(record.warehouses)) == len(record.warehouses) == 2 * len(queries.MEASURED_SCENARIOS)


def test_a_warehouse_whose_settings_drifted_stops_the_run(make_cursor, make_connection):
    clock = FakeClock()
    # The readback right after CREATE reports acceleration on.
    cur = make_cursor(responses=[show_row(qas=True) for _ in range(2000)])
    with pytest.raises(ValueError, match="ENABLE_QUERY_ACCELERATION"):
        scenarios.run_experiment(
            make_connection(cur), replicates=1, include_natural=False, clock=clock, sleep=clock.sleep
        )


def test_a_rejected_multi_cluster_warehouse_names_the_edition(make_connection):
    class Rejecting:
        description = [("col",)]

        def __init__(self):
            self.executed = []

        def execute(self, sql):
            self.executed.append(sql)
            if sql.startswith("CREATE WAREHOUSE"):
                raise RuntimeError("Multi-cluster warehouses are not enabled")
            return self

        def fetchall(self):
            return [("ACC", "REGION", "9.0.0", None)]

        def close(self):
            pass

    with pytest.raises(ValueError, match="Enterprise"):
        scenarios.run_experiment(make_connection(Rejecting()), replicates=1, include_natural=False)


def test_an_unrelated_ddl_error_is_not_blamed_on_the_edition(make_connection):
    # The account rejected RESOURCE_CONSTRAINT on syntax grounds; appending the
    # edition hint to that sent the reader looking in the wrong place.
    class Rejecting:
        description = [("col",)]

        def execute(self, sql):
            if sql.startswith("CREATE WAREHOUSE"):
                raise RuntimeError("000682 (22000): Cannot set resource constraint to 'STANDARD_GEN_2'.")
            return self

        def fetchall(self):
            return [("ACC", "REGION", "9.0.0", None)]

        def close(self):
            pass

    with pytest.raises(ValueError) as caught:
        scenarios.run_experiment(make_connection(Rejecting()), replicates=1, include_natural=False)
    assert "Cannot set resource constraint" in str(caught.value)
    assert "Enterprise" not in str(caught.value)


def test_drop_warehouses_drops_exactly_what_it_is_given(make_cursor, make_connection):
    cursor = make_cursor()
    conn = make_connection(cursor)
    messages: list[str] = []

    scenarios.drop_warehouses(conn, warehouses=["A_WH", "B_WH"], echo=messages.append)

    assert cursor.executed == ["DROP WAREHOUSE IF EXISTS A_WH", "DROP WAREHOUSE IF EXISTS B_WH"]
    assert messages == ["Dropped A_WH.", "Dropped B_WH."]
