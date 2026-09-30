"""Unit tests for the billing test's SQL and constants."""

from __future__ import annotations

import pytest

from experiments.multi_cluster_billing.core import queries


def test_published_rate_applies_the_gen2_multiplier():
    assert queries.published_credits_per_hour("XSMALL", queries.STANDARD_GEN_1) == pytest.approx(1.0)
    assert queries.published_credits_per_hour("XSMALL", queries.STANDARD_GEN_2) == pytest.approx(1.35)
    assert queries.published_credits_per_hour("small", queries.STANDARD_GEN_2) == pytest.approx(2.7)


def test_published_rate_rejects_unknown_size_and_generation():
    with pytest.raises(ValueError, match="size"):
        queries.published_credits_per_hour("HUGE", queries.STANDARD_GEN_2)
    with pytest.raises(ValueError, match="resource constraint"):
        queries.published_credits_per_hour("XSMALL", "STANDARD_GEN_9")


def test_an_extra_cluster_lives_from_its_scale_out_to_the_suspend():
    # The whole design rests on this: a scenario picks when the extra cluster
    # starts, and how long it lives follows from when the warehouse stops.
    for spec in queries.EXTRA_SCENARIOS:
        assert spec.extra_cluster_seconds == spec.cycle_seconds - spec.scale_out_at_seconds
    # A scenario with no extra cluster has no extra lifetime to charge for.
    assert queries.SHORT.extra_cluster_seconds == queries.CONTROL.extra_cluster_seconds == 0


def test_the_measured_scenarios_are_the_seven_the_questions_need():
    assert [s.name for s in queries.MEASURED_SCENARIOS] == [
        "short",
        "control",
        "inside",
        "brief",
        "nearly",
        "outlives",
        "k5",
    ]
    assert [s.target_clusters for s in queries.MEASURED_SCENARIOS] == [1, 1, 2, 2, 2, 2, 5]
    assert [s.scale_out_at_seconds for s in queries.MEASURED_SCENARIOS] == [25, 70, 10, 70, 65, 60, 70]
    assert [s.cycle_seconds for s in queries.MEASURED_SCENARIOS] == [45, 90, 45, 90, 120, 150, 90]
    assert all(s.kind == "forced" for s in queries.MEASURED_SCENARIOS)
    assert all(s.max_concurrency_level is None for s in queries.MEASURED_SCENARIOS)


def test_only_one_scenario_starts_its_extra_inside_the_first_minute():
    early = [s for s in queries.EXTRA_SCENARIOS if s.scale_out_at_seconds < queries.MINIMUM_SECONDS]
    assert [s.name for s in early] == [queries.INSIDE.name]


def test_only_one_scenario_lets_its_extra_outlive_the_minimum():
    long_lived = [s for s in queries.EXTRA_SCENARIOS if s.extra_cluster_seconds > queries.MINIMUM_SECONDS]
    assert [s.name for s in long_lived] == [queries.OUTLIVES.name]


def test_the_two_scenarios_that_read_the_premise_and_the_decoding_run_alone():
    assert queries.SHORT.reads == queries.READS_PREMISE
    assert queries.SHORT.cycle_seconds < queries.MINIMUM_SECONDS
    assert queries.CONTROL.reads == queries.READS_RATE
    assert queries.CONTROL.cycle_seconds > queries.MINIMUM_SECONDS
    assert queries.SHORT.target_clusters == queries.CONTROL.target_clusters == 1


def test_natural_is_last_and_serialises_queries():
    assert queries.ALL_SCENARIOS[-1] is queries.NATURAL
    assert queries.NATURAL.kind == "natural"
    assert queries.NATURAL.max_concurrency_level == 1


def test_warehouse_name_carries_scenario_replicate_and_token():
    assert queries.warehouse_name("KEEBO_MCB", "brief", 3, "20260814T120000") == "KEEBO_MCB_BRIEF_R3_20260814T120000"


def test_warehouse_name_rejects_injection():
    with pytest.raises(ValueError):
        queries.warehouse_name("KEEBO_MCB; DROP", "brief", 1, "20260814T120000")


def test_every_warehouse_pins_the_same_settings():
    sql = queries.create_warehouse_sql(
        "KEEBO_MCB_BRIEF_R1_T", size="XSMALL", spec=queries.BRIEF, resource_constraint=queries.STANDARD_GEN_2
    )
    for setting in (
        "WAREHOUSE_SIZE = XSMALL",
        "WAREHOUSE_TYPE = STANDARD",
        "GENERATION = '2'",
        "SCALING_POLICY = STANDARD",
        "MIN_CLUSTER_COUNT = 1",
        "MAX_CLUSTER_COUNT = 5",
        "AUTO_SUSPEND = 3600",
        "AUTO_RESUME = FALSE",
        "INITIALLY_SUSPENDED = TRUE",
        "ENABLE_QUERY_ACCELERATION = FALSE",
    ):
        assert setting in sql


def test_max_cluster_count_is_five_even_for_single_cluster_scenarios():
    # The scenarios must differ in MIN_CLUSTER_COUNT only; v1 varied MAX too.
    for spec in queries.MEASURED_SCENARIOS:
        sql = queries.create_warehouse_sql(
            "KEEBO_MCB_X_R1_T", size="XSMALL", spec=spec, resource_constraint=queries.STANDARD_GEN_2
        )
        assert "MAX_CLUSTER_COUNT = 5" in sql


def test_only_the_natural_warehouse_serialises_queries():
    forced = queries.create_warehouse_sql(
        "KEEBO_MCB_BRIEF_R1_T", size="XSMALL", spec=queries.BRIEF, resource_constraint=queries.STANDARD_GEN_2
    )
    natural = queries.create_warehouse_sql(
        "KEEBO_MCB_NATURAL_R1_T", size="XSMALL", spec=queries.NATURAL, resource_constraint=queries.STANDARD_GEN_2
    )
    assert "MAX_CONCURRENCY_LEVEL" not in forced
    assert "MAX_CONCURRENCY_LEVEL = 1" in natural


def test_the_generation_is_set_with_GENERATION_not_RESOURCE_CONSTRAINT():
    # Newer accounts reject RESOURCE_CONSTRAINT outright: "Use the GENERATION
    # property to set warehouse hardware generation." GENERATION is accepted
    # everywhere and is the documented form, so it is the only one emitted.
    for constraint, digit in ((queries.STANDARD_GEN_1, "'1'"), (queries.STANDARD_GEN_2, "'2'")):
        sql = queries.create_warehouse_sql("W", size="XSMALL", spec=queries.BRIEF, resource_constraint=constraint)
        assert f"GENERATION = {digit}" in sql
        assert "RESOURCE_CONSTRAINT" not in sql


def test_create_warehouse_rejects_a_bad_size_and_generation():
    with pytest.raises(ValueError, match="size"):
        queries.create_warehouse_sql("W", size="HUGE", spec=queries.BRIEF, resource_constraint=queries.STANDARD_GEN_2)
    with pytest.raises(ValueError, match="resource constraint"):
        queries.create_warehouse_sql("W", size="XSMALL", spec=queries.BRIEF, resource_constraint="GEN_9")


def test_natural_query_is_uncacheable_and_sized():
    sql = queries.natural_query(1234)
    assert "GENERATOR(ROWCOUNT => 1234)" in sql
    assert "RANDOM()" in sql  # non-deterministic, so the result cache can't serve it


def test_natural_query_rejects_non_positive_rowcount():
    with pytest.raises(ValueError, match="rowcount"):
        queries.natural_query(0)


def test_warehouse_consistent_sorts_last_within_a_timestamp():
    # It is the completion marker; every other event may be the start of a
    # transition, so it must follow whatever it completes.
    ranks = queries.EVENT_PHASE_RANK
    assert ranks["WAREHOUSE_CONSISTENT"] > max(v for k, v in ranks.items() if k != "WAREHOUSE_CONSISTENT")
    assert ranks["RESUME_WAREHOUSE"] < ranks["ALTER_WAREHOUSE"] < ranks["SPINUP_CLUSTER"]
    assert ranks["SPINUP_CLUSTER"] < ranks["RESUME_CLUSTER"]
    assert ranks["RESUME_CLUSTER"] == ranks["SUSPEND_CLUSTER"]
    assert queries.UNKNOWN_PHASE_RANK < ranks["WAREHOUSE_CONSISTENT"]


def test_phase_rank_case_covers_every_known_event():
    case = queries.phase_rank_case("event_name")
    for name, rank in queries.EVENT_PHASE_RANK.items():
        assert f"WHEN '{name}' THEN {rank}" in case
    assert f"ELSE {queries.UNKNOWN_PHASE_RANK}" in case


def test_events_sql_projects_event_state_and_orders_deterministically():
    sql = queries.EVENTS_SQL.format(names="'W'", window_start="s", window_end="e")
    assert "event_state" in sql
    ordering = sql.split("ORDER BY", 1)[1]
    assert ordering.index("timestamp") < ordering.index("CASE")
    assert ordering.index("CASE") < ordering.index("cluster_number")
