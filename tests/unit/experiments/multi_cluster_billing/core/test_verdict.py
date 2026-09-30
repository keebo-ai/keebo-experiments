"""Unit tests for the verdict arithmetic."""

from __future__ import annotations

import pytest

from experiments.multi_cluster_billing.core import queries, verdict
from experiments.multi_cluster_billing.core.questions import (
    OWN_MINUTE,
    PER_SECOND,
    RULE_TEXT,
    RULES,
    SHARES_THE_MINUTE,
    WHOLE_MINUTES,
)


def test_cluster_seconds_converts_at_the_rate_it_is_given():
    # 0.171750 credits at the Gen2 XSMALL rate is 458 cluster-seconds; at the
    # Gen1 rate it decodes to 618.3, which is the error that made v1
    # INCONCLUSIVE.
    assert verdict.cluster_seconds(0.171750, 1.35) == pytest.approx(458.0)
    assert verdict.cluster_seconds(0.171750, 1.0) == pytest.approx(618.3)


def test_cluster_seconds_rejects_a_nonpositive_rate():
    with pytest.raises(ValueError, match="rate"):
        verdict.cluster_seconds(1.0, 0.0)


# --------------------------------------------------------------------------- #
# What each rule charges for one extra cluster
# --------------------------------------------------------------------------- #
def extra(start_offset: float, seconds: float) -> verdict.Extra:
    return verdict.Extra(start_offset=start_offset, seconds=seconds)


def test_per_second_charges_exactly_what_the_cluster_ran():
    assert verdict.charge(PER_SECOND, extra(70.0, 20.0)) == pytest.approx(20.0)
    assert verdict.charge(PER_SECOND, extra(10.0, 95.5)) == pytest.approx(95.5)


def test_own_minute_charges_a_minute_then_by_the_second():
    assert verdict.charge(OWN_MINUTE, extra(70.0, 20.0)) == pytest.approx(60.0)
    assert verdict.charge(OWN_MINUTE, extra(70.0, 55.0)) == pytest.approx(60.0)
    assert verdict.charge(OWN_MINUTE, extra(60.0, 90.0)) == pytest.approx(90.0)


def test_whole_minutes_rounds_up_to_the_next_boundary():
    assert verdict.charge(WHOLE_MINUTES, extra(70.0, 20.0)) == pytest.approx(60.0)
    assert verdict.charge(WHOLE_MINUTES, extra(60.0, 61.0)) == pytest.approx(120.0)
    assert verdict.charge(WHOLE_MINUTES, extra(60.0, 90.0)) == pytest.approx(120.0)


def test_shares_the_minute_charges_only_what_runs_past_the_warehouses_first_minute():
    # Started at 10s, ran 35s: gone by 45s, inside a minute already paid for.
    assert verdict.charge(SHARES_THE_MINUTE, extra(10.0, 35.0)) == pytest.approx(0.0)
    # Started at 10s, ran 90s: the first 50 are covered, the other 40 are not.
    assert verdict.charge(SHARES_THE_MINUTE, extra(10.0, 90.0)) == pytest.approx(40.0)
    # Started at 70s, after the covered minute ended, so all 20 seconds count.
    # The 10 seconds between the minute ending and this cluster starting belong
    # to no cluster and are charged to nobody.
    assert verdict.charge(SHARES_THE_MINUTE, extra(70.0, 20.0)) == pytest.approx(20.0)


def test_shares_the_minute_only_ever_differs_from_per_second_before_the_minute_is_up():
    # Once the warehouse's minute is spent there is no minimum left to share, so
    # "one minimum for the warehouse" and "no minimum at all" charge the same
    # thing. The only place they can disagree is a cluster that overlaps the
    # first minute, which is why `inside` exists.
    for start_offset in (60.0, 61.0, 120.0):
        for seconds in (5.0, 20.0, 90.0):
            late = extra(start_offset, seconds)
            assert verdict.charge(SHARES_THE_MINUTE, late) == verdict.charge(PER_SECOND, late)
    early = extra(10.0, 35.0)
    assert verdict.charge(SHARES_THE_MINUTE, early) != verdict.charge(PER_SECOND, early)


def test_shares_the_minute_is_the_only_rule_that_reads_the_start_offset():
    early, late = extra(10.0, 35.0), extra(70.0, 35.0)
    for rule in (PER_SECOND, OWN_MINUTE, WHOLE_MINUTES):
        assert verdict.charge(rule, early) == verdict.charge(rule, late)
    assert verdict.charge(SHARES_THE_MINUTE, early) != verdict.charge(SHARES_THE_MINUTE, late)


def test_charge_rejects_a_rule_it_does_not_know():
    with pytest.raises(ValueError, match="rule"):
        verdict.charge("PER_VIBES", extra(0.0, 10.0))


# --------------------------------------------------------------------------- #
# Whole-replicate predictions
# --------------------------------------------------------------------------- #
def test_every_rule_charges_one_warehouse_minimum_and_nothing_else():
    for rule in RULES:
        assert verdict.predict(rule, warehouse_seconds=45.0, extras=[]) == pytest.approx(60.0)
        assert verdict.predict(rule, warehouse_seconds=90.0, extras=[]) == pytest.approx(90.0)


def test_a_prediction_is_rounded_up_to_the_whole_second():
    # The meter charges whole seconds. control ran 90.136 and was billed 91;
    # taking that rounding for a rate error is how v2 reached no answer.
    assert verdict.predict(OWN_MINUTE, warehouse_seconds=90.136, extras=[]) == 91.0
    assert verdict.predict(OWN_MINUTE, warehouse_seconds=89.984, extras=[extra(70.0, 20.0)] * 4) == 330.0


def test_the_scenarios_separate_every_pair_of_rules():
    """No two rules predict the same thing everywhere — else no run can decide."""
    charges = {
        rule: tuple(
            sum(
                verdict.charge(rule, extra(float(spec.scale_out_at_seconds), float(spec.extra_cluster_seconds)))
                for _ in range(spec.target_clusters - 1)
            )
            for spec in queries.MEASURED_SCENARIOS
        )
        for rule in RULES
    }
    assert len(set(charges.values())) == len(RULES)


def test_only_inside_can_tell_a_shared_minute_from_no_minute_at_all():
    def charged(rule, spec):
        return verdict.charge(rule, extra(float(spec.scale_out_at_seconds), float(spec.extra_cluster_seconds)))

    extras_run = [spec for spec in queries.MEASURED_SCENARIOS if spec.target_clusters > 1]
    # Every other scenario starts its extra clusters after the warehouse's first
    # minute is already spent, and there the two rules charge the same thing.
    caught = [spec.name for spec in extras_run if charged(SHARES_THE_MINUTE, spec) != charged(PER_SECOND, spec)]
    assert caught == [queries.INSIDE.name]
    # It is also the only scenario in which any rule makes a cluster free.
    free = [spec.name for spec in extras_run if charged(SHARES_THE_MINUTE, spec) == 0]
    assert free == [queries.INSIDE.name]


def test_every_scenario_with_a_short_extra_cluster_catches_a_per_cluster_minute():
    def charged(rule, spec):
        return verdict.charge(rule, extra(float(spec.scale_out_at_seconds), float(spec.extra_cluster_seconds)))

    extras_run = [spec for spec in queries.MEASURED_SCENARIOS if spec.target_clusters > 1]
    caught = [spec.name for spec in extras_run if charged(SHARES_THE_MINUTE, spec) != charged(OWN_MINUTE, spec)]
    # All but `outlives`, whose extra cluster runs past a minute on its own: a
    # minute of its own and no minute at all cost the same once it does.
    assert caught == [queries.INSIDE.name, queries.BRIEF.name, queries.NEARLY.name, queries.K5.name]


def test_outlives_is_the_only_scenario_that_catches_whole_minutes():
    caught = [
        spec.name
        for spec in queries.MEASURED_SCENARIOS
        if spec.target_clusters > 1
        and verdict.charge(WHOLE_MINUTES, extra(float(spec.scale_out_at_seconds), float(spec.extra_cluster_seconds)))
        != verdict.charge(OWN_MINUTE, extra(float(spec.scale_out_at_seconds), float(spec.extra_cluster_seconds)))
    ]
    assert caught == [queries.OUTLIVES.name]


# --------------------------------------------------------------------------- #
# Observations and the verdict
# --------------------------------------------------------------------------- #
def observation(scenario, index, *, warehouse_seconds, extras, billed):
    return verdict.Observation(
        scenario=scenario,
        index=index,
        warehouse=f"W_{scenario.upper()}_R{index}",
        warehouse_seconds=warehouse_seconds,
        extras=list(extras),
        billed_seconds=billed,
    )


def shape(spec: queries.ScenarioSpec, *, jitter: float) -> tuple[float, list[verdict.Extra]]:
    """The lifetimes one replicate of `spec` would produce, roughly."""
    warehouse_seconds = float(spec.cycle_seconds) + jitter
    extras = [
        extra(float(spec.scale_out_at_seconds) + jitter, float(spec.extra_cluster_seconds))
        for _ in range(spec.target_clusters - 1)
    ]
    return warehouse_seconds, extras


def observations_for(rule, *, replicates=4, specs=None):
    """Replicates of every measured scenario, billed as `rule` says."""
    out = []
    for spec in specs or queries.MEASURED_SCENARIOS:
        for index in range(replicates):
            warehouse_seconds, extras = shape(spec, jitter=0.1 * index)
            out.append(
                observation(
                    spec.name,
                    index + 1,
                    warehouse_seconds=warehouse_seconds,
                    extras=extras,
                    billed=verdict.predict(rule, warehouse_seconds=warehouse_seconds, extras=extras),
                )
            )
    return out


def test_a_replicate_reports_what_the_extra_clusters_added():
    item = observation("brief", 1, warehouse_seconds=90.1, extras=[extra(70.0, 20.0)], billed=151.0)
    assert item.base_seconds == 91.0
    assert item.extra_charge_seconds == pytest.approx(60.0)
    assert item.predicted_extra_charge[OWN_MINUTE] == pytest.approx(60.0)
    assert item.predicted_extra_charge[PER_SECOND] == pytest.approx(20.0)


def test_a_bill_within_a_second_of_a_prediction_matches_it():
    # The meter charges in whole seconds, so a lifetime read from the event log
    # lands somewhere inside the second the meter rounded up to.
    item = observation("brief", 1, warehouse_seconds=90.1, extras=[extra(70.0, 20.0)], billed=151.0)
    assert item.matches(OWN_MINUTE)
    assert not item.matches(PER_SECOND)

    off_by_one = observation("brief", 1, warehouse_seconds=90.1, extras=[extra(70.0, 20.0)], billed=152.0)
    assert off_by_one.matches(OWN_MINUTE)

    off_by_two = observation("brief", 1, warehouse_seconds=90.1, extras=[extra(70.0, 20.0)], billed=153.0)
    assert not off_by_two.matches(OWN_MINUTE)


@pytest.mark.parametrize("rule", list(RULES))
def test_each_rule_is_elected_by_bills_that_follow_it(rule):
    result = verdict.compute_verdict(observations_for(rule))
    assert result.outcome == rule
    assert result.fits == [rule]


def test_losing_every_scenario_with_a_short_extra_cluster_settles_almost_nothing():
    # Four of the seven have an extra cluster that dies inside its first minute,
    # and they are the ones that have to hit a cluster count, so losing all four
    # is a plausible bad night. What is left is `outlives`, where three of the
    # four rules predict the same 90 seconds and only rounding up to whole
    # minutes is contradicted.
    lost = {queries.INSIDE, queries.BRIEF, queries.NEARLY, queries.K5}
    without = [spec for spec in queries.MEASURED_SCENARIOS if spec not in lost]
    result = verdict.compute_verdict(observations_for(OWN_MINUTE, specs=without))
    assert result.outcome == verdict.INCONCLUSIVE
    assert set(result.fits) == {PER_SECOND, OWN_MINUTE, SHARES_THE_MINUTE}
    # The reader is left with a scenario name to re-run rather than a dead end.
    assert f"{queries.K5.name} is the scenario built to tell them apart" in result.reason


def test_dropping_outlives_leaves_whole_minutes_standing():
    without = [spec for spec in queries.MEASURED_SCENARIOS if spec is not queries.OUTLIVES]
    result = verdict.compute_verdict(observations_for(OWN_MINUTE, specs=without))
    assert result.outcome == verdict.INCONCLUSIVE
    assert set(result.fits) == {OWN_MINUTE, WHOLE_MINUTES}
    assert f"{queries.OUTLIVES.name} is the scenario built to tell them apart" in result.reason


def test_a_bill_matching_nothing_says_so_rather_than_rounding_to_the_nearest():
    off = [
        observation(
            o.scenario, o.index, warehouse_seconds=o.warehouse_seconds, extras=o.extras, billed=o.billed_seconds + 25.0
        )
        for o in observations_for(OWN_MINUTE)
    ]
    result = verdict.compute_verdict(off)
    assert result.outcome == verdict.NO_RULE_FITS
    assert result.fits == []


def test_an_empty_observation_set_is_inconclusive():
    result = verdict.compute_verdict([])
    assert result.outcome == verdict.INCONCLUSIVE
    assert "no replicates" in result.reason


def test_the_cross_check_is_reported_but_does_not_decide():
    # `natural` billed as WHOLE_MINUTES while every forced scenario billed as
    # OWN_MINUTE: the verdict is still OWN_MINUTE, and natural is still shown.
    observations = observations_for(OWN_MINUTE)
    warehouse_seconds, extras = 95.0, [extra(12.0, 61.0)]
    observations.append(
        observation(
            queries.NATURAL.name,
            1,
            warehouse_seconds=warehouse_seconds,
            extras=extras,
            billed=verdict.predict(WHOLE_MINUTES, warehouse_seconds=warehouse_seconds, extras=extras),
        )
    )
    result = verdict.compute_verdict(observations)
    assert result.outcome == OWN_MINUTE
    assert queries.NATURAL.name in [scenario.name for scenario in result.scenarios]
    assert queries.NATURAL.name not in [scenario.name for scenario in verdict.deciding(result.scenarios)]


def test_a_run_of_only_the_cross_check_decides_nothing():
    warehouse_seconds, extras = 95.0, [extra(12.0, 40.0)]
    result = verdict.compute_verdict(
        [
            observation(
                queries.NATURAL.name,
                1,
                warehouse_seconds=warehouse_seconds,
                extras=extras,
                billed=verdict.predict(OWN_MINUTE, warehouse_seconds=warehouse_seconds, extras=extras),
            )
        ]
    )
    assert result.outcome == verdict.INCONCLUSIVE
    assert "cross-check" in result.reason


def test_scenarios_that_cannot_discriminate_do_not_rule_anything_out():
    result = verdict.compute_verdict(observations_for(OWN_MINUTE))
    for name in (queries.SHORT.name, queries.CONTROL.name):
        scenario = next(s for s in result.scenarios if s.name == name)
        assert set(scenario.fits) == set(RULES)
        assert scenario.rules_out == []


# --------------------------------------------------------------------------- #
# The checks that stand in front of the answer
# --------------------------------------------------------------------------- #
def test_bills_that_decode_to_whole_seconds_pass_the_meter_check():
    check = verdict.check_meter(observations_for(OWN_MINUTE), size="XSMALL", resource_constraint=queries.STANDARD_GEN_2)
    assert check.published == pytest.approx(1.35)
    assert check.worst_gap_seconds == pytest.approx(0.0)
    assert check.ok


def test_bills_decoded_at_the_wrong_rate_fail_the_meter_check():
    # What a Gen2 bill read at the Gen1 rate looks like: 35% larger, and no
    # longer landing on whole seconds.
    off = [
        observation(
            o.scenario, o.index, warehouse_seconds=o.warehouse_seconds, extras=o.extras, billed=o.billed_seconds * 1.35
        )
        for o in observations_for(OWN_MINUTE)
    ]
    check = verdict.check_meter(off, size="XSMALL", resource_constraint=queries.STANDARD_GEN_1)
    assert not check.ok
    assert check.worst_gap_seconds > queries.WHOLE_SECOND_TOLERANCE
    assert check.worst_warehouse is not None


def test_the_meter_check_needs_something_to_check():
    check = verdict.check_meter([], size="XSMALL", resource_constraint=queries.STANDARD_GEN_2)
    assert not check.ok
    assert check.n == 0


def test_the_minimum_premise_holds_when_short_bills_sixty():
    check = verdict.check_minimum(
        [observation("short", i + 1, warehouse_seconds=45.0, extras=[], billed=60.0) for i in range(4)]
    )
    assert check.mean_billed == pytest.approx(60.0)
    assert check.mean_warehouse_seconds == pytest.approx(45.0)
    assert check.holds


def test_the_minimum_premise_fails_when_short_bills_what_it_used():
    check = verdict.check_minimum(
        [observation("short", i + 1, warehouse_seconds=45.0, extras=[], billed=45.0) for i in range(4)]
    )
    assert not check.holds


def test_the_minimum_premise_fails_when_short_never_ran():
    assert not verdict.check_minimum([]).holds


# --------------------------------------------------------------------------- #
# The plain-English answer
# --------------------------------------------------------------------------- #
def explanation(result, **kwargs) -> str:
    return "\n\n".join(verdict.explain(result, **kwargs))


def test_the_answer_is_stated_in_plain_english():
    text = explanation(verdict.compute_verdict(observations_for(OWN_MINUTE)))
    assert "full minute" in text


def test_the_plain_english_never_leaks_a_rule_constant():
    # The reader of this section should not have to know the vocabulary.
    for rule in RULES:
        text = explanation(verdict.compute_verdict(observations_for(rule)))
        assert not any(name in text for name in RULES)
        assert "_" not in text.replace("scale-out", "")


def test_the_answer_says_what_it_costs():
    own_minute = explanation(verdict.compute_verdict(observations_for(OWN_MINUTE)))
    per_second = explanation(verdict.compute_verdict(observations_for(PER_SECOND)))
    assert "bill" in own_minute.lower()
    assert "bill" in per_second.lower()
    assert own_minute != per_second


def test_the_answer_quotes_the_scenario_that_contradicted_each_other_rule():
    text = explanation(verdict.compute_verdict(observations_for(OWN_MINUTE)))
    for name in (queries.INSIDE.name, queries.OUTLIVES.name, queries.K5.name):
        assert name in text


def test_a_run_that_cannot_choose_says_so_and_says_what_would():
    without = [spec for spec in queries.MEASURED_SCENARIOS if spec is not queries.OUTLIVES]
    text = explanation(verdict.compute_verdict(observations_for(OWN_MINUTE, specs=without)))
    assert "cannot choose" in text.lower()
    assert queries.OUTLIVES.name in text


def test_a_bill_matching_nothing_says_the_answer_is_none_of_the_four():
    off = [
        observation(
            o.scenario, o.index, warehouse_seconds=o.warehouse_seconds, extras=o.extras, billed=o.billed_seconds + 25.0
        )
        for o in observations_for(OWN_MINUTE)
    ]
    text = explanation(verdict.compute_verdict(off))
    assert "none of the four" in text.lower()
    assert "25" in text  # how far off the closest rule was


def test_a_failed_premise_leads_the_explanation():
    result = verdict.compute_verdict(observations_for(OWN_MINUTE))
    broken = verdict.MinimumCheck(mean_billed=45.0, mean_warehouse_seconds=45.0, n=4, holds=False)
    paragraphs = verdict.explain(result, minimum=broken)
    assert "no 60-second minimum" in paragraphs[0]
    assert "45" in paragraphs[0]


def test_a_holding_premise_is_stated_once_and_not_belaboured():
    result = verdict.compute_verdict(observations_for(OWN_MINUTE))
    holds = verdict.MinimumCheck(mean_billed=60.1, mean_warehouse_seconds=45.0, n=4, holds=True)
    text = "\n\n".join(verdict.explain(result, minimum=holds))
    assert "45" in text and "60" in text


# --------------------------------------------------------------------------- #
# Per-scenario conclusions
# --------------------------------------------------------------------------- #
def scenario_named(name: str, rule=OWN_MINUTE) -> verdict.ScenarioResult:
    result = verdict.compute_verdict(observations_for(rule))
    return next(s for s in result.scenarios if s.name == name)


def test_shorts_conclusion_speaks_to_the_minimum_and_nothing_else():
    text = verdict.scenario_conclusion(scenario_named(queries.SHORT.name))
    assert "minimum is real" in text
    assert "45" in text and "60" in text


def test_controls_conclusion_speaks_to_the_decoding():
    text = verdict.scenario_conclusion(scenario_named(queries.CONTROL.name))
    assert "seconds correctly" in text


def test_a_measuring_scenario_says_what_its_numbers_rule_out():
    text = verdict.scenario_conclusion(scenario_named(queries.INSIDE.name))
    assert "rules out 2 of the four" in text
    assert "35" in text and "60" in text


def test_a_scenario_that_leaves_several_rules_standing_says_it_failed_to_separate_them():
    # `outlives` is the case that made this worth wording carefully. Its extra
    # cluster runs past a minute on its own, and three of the four rules then
    # charge the same 90 seconds for it, so it settles only that the bill is not
    # rounded to whole minutes. Printed as a bare list under "Conclusion", those
    # three sentences read like findings — including "no minimum of its own",
    # which this very run rules out elsewhere.
    text = verdict.scenario_conclusion(scenario_named(queries.OUTLIVES.name))
    assert "rules out 1 of the four" in text
    assert "cannot choose between the other three" in text
    # The rules it could not separate are still named, but as a group that agrees
    # on this scenario's number rather than as three separate conclusions.
    assert "predict that same figure" in text
    for rule in (PER_SECOND, OWN_MINUTE, SHARES_THE_MINUTE):
        assert RULE_TEXT[rule].says in text
    assert RULE_TEXT[WHOLE_MINUTES].says in text  # named as the one it rules out


def test_a_scenario_that_cannot_decide_points_at_the_scenarios_that_can():
    # The reader is given somewhere to go rather than a shortlist and nothing
    # else. `k5` leads because it separates these three by the widest margin.
    text = verdict.scenario_conclusion(scenario_named(queries.OUTLIVES.name))
    assert f"settled by {queries.K5.name}, {queries.INSIDE.name}" in text
    assert f"and {queries.NEARLY.name}, not here" in text
    assert queries.OUTLIVES.name not in text.split("settled by")[1]

    # And the other way round: the two rules that agree everywhere except past
    # the first minute are separated only by `outlives`, so that is the only
    # name `inside` can offer.
    inside = verdict.scenario_conclusion(scenario_named(queries.INSIDE.name))
    assert "cannot choose between the other two" in inside
    assert f"settled by {queries.OUTLIVES.name}, not here" in inside


def test_a_scenario_that_settles_it_outright_makes_no_such_hedge():
    # Only when one rule is left standing does a conclusion name a rule on its
    # own. Billed in whole minutes, `outlives`'s 90-second cluster costs 120,
    # which no other rule predicts, and the same scenario that hedges above
    # settles it here.
    text = verdict.scenario_conclusion(scenario_named(queries.OUTLIVES.name, rule=WHOLE_MINUTES))
    assert "rules out 3 of the four" in text
    assert f"What is left here: {RULE_TEXT[WHOLE_MINUTES].says}." in text
    assert "cannot choose between" not in text and "settled by" not in text


def test_the_cross_checks_conclusion_says_it_confirms_rather_than_decides():
    observations = observations_for(OWN_MINUTE)
    warehouse_seconds, extras = 95.0, [extra(12.0, 40.0)]
    observations.append(
        observation(
            queries.NATURAL.name,
            1,
            warehouse_seconds=warehouse_seconds,
            extras=extras,
            billed=verdict.predict(OWN_MINUTE, warehouse_seconds=warehouse_seconds, extras=extras),
        )
    )
    result = verdict.compute_verdict(observations)
    natural = next(s for s in result.scenarios if s.name == queries.NATURAL.name)
    text = verdict.scenario_conclusion(natural)
    assert "MIN_CLUSTER_COUNT" in text
    assert "billed the same way" in text


def test_every_question_gets_an_answer_from_the_rules_that_fit():
    result = verdict.compute_verdict(observations_for(OWN_MINUTE))
    answers = result.answers
    assert len(answers) == 4
    for question, text in answers:
        assert question.text.endswith("?")
        assert text and not any(name in text for name in RULES)
