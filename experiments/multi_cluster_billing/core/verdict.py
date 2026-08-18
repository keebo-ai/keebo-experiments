"""The arithmetic: four candidate rules, one bill per replicate, one answer.

:mod:`questions` names the four ways Snowflake could charge for a cluster beyond
the first and says in words what each would mean. This module turns each of them
into a number, compares that number with what the account was actually billed,
and reports which rules are left.

Pure functions only: no connection, no I/O, no clock.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from experiments.multi_cluster_billing.core import queries, questions
from experiments.multi_cluster_billing.core.questions import (
    OWN_MINUTE,
    PER_SECOND,
    RULE_TEXT,
    RULES,
    SHARES_THE_MINUTE,
    WHOLE_MINUTES,
)

#: More than one rule still fits every scenario. A shorter list than we started
#: with, but not an answer.
INCONCLUSIVE = "INCONCLUSIVE"

#: No rule fits. Not a partial answer — a sign that something outside all four
#: is in the bill, starting with the way credits were decoded.
NO_RULE_FITS = "NO_RULE_FITS"


def cluster_seconds(credits: float, credits_per_hour: float) -> float:
    """Turn billed credits into seconds of running cluster at an hourly rate."""
    if float(credits_per_hour) <= 0:
        raise ValueError(f"rate must be positive, got {credits_per_hour!r}")
    return float(credits) * queries.SECONDS_PER_HOUR / float(credits_per_hour)


@dataclass(frozen=True)
class Extra:
    """One cluster beyond the first: when it started, and how long it ran.

    ``start_offset`` is seconds after the warehouse itself started. Three of the
    four rules ignore it; the fourth is the reason it is carried.
    """

    start_offset: float
    seconds: float


def charge(rule: str, extra: Extra) -> float:
    """Seconds this rule says one extra cluster adds to the bill."""
    minimum = queries.MINIMUM_SECONDS
    seconds = float(extra.seconds)

    if rule == PER_SECOND:
        return seconds
    if rule == OWN_MINUTE:
        return max(minimum, seconds)
    if rule == WHOLE_MINUTES:
        return minimum * math.ceil(seconds / minimum)
    if rule == SHARES_THE_MINUTE:
        # There is one minimum, and the warehouse already paid it. This cluster
        # is charged by the second for whatever part of it runs past that first
        # minute, so a cluster that finishes inside the minute is free and one
        # that starts after it is charged in full. `covered` is the part of the
        # cluster's own life that overlaps the paid minute, which is why this is
        # the only rule that reads `start_offset`.
        covered = max(0.0, min(seconds, minimum - float(extra.start_offset)))
        return seconds - covered
    raise ValueError(f"rule must be one of {list(RULES)}, got {rule!r}")


def predict(rule: str, *, warehouse_seconds: float, extras: list[Extra]) -> float:
    """Whole seconds this rule says the whole replicate costs.

    Every rule agrees on the first cluster: the warehouse carries a minute at the
    least. They differ only in what they add for the clusters beyond it. The
    total is rounded up because the meter bills whole seconds.
    """
    base = max(queries.MINIMUM_SECONDS, float(warehouse_seconds))
    return float(math.ceil(base + sum(charge(rule, extra) for extra in extras)))


@dataclass(frozen=True)
class Observation:
    """One replicate: what it ran, and what the account was charged for it.

    ``warehouse_seconds`` and ``extras`` come from the event log, never from the
    poll clock. ``billed_seconds`` is the metering row decoded at the published
    rate.
    """

    scenario: str
    index: int
    warehouse: str
    warehouse_seconds: float
    extras: list[Extra]
    billed_seconds: float

    @property
    def base_seconds(self) -> float:
        """What the first cluster alone would cost: a minute, or the time it ran."""
        return float(math.ceil(max(queries.MINIMUM_SECONDS, self.warehouse_seconds)))

    @property
    def extra_charge_seconds(self) -> float:
        """What the clusters beyond the first added to the bill.

        The bill minus what one cluster alone would have cost. Good to within a
        second, which is as fine as the meter measures anything.
        """
        return self.billed_seconds - self.base_seconds

    @property
    def predictions(self) -> dict[str, float]:
        return {rule: predict(rule, warehouse_seconds=self.warehouse_seconds, extras=self.extras) for rule in RULES}

    @property
    def predicted_extra_charge(self) -> dict[str, float]:
        """Each rule's answer to "what did the extra clusters add?"."""
        base = self.base_seconds
        return {rule: value - base for rule, value in self.predictions.items()}

    @property
    def misses(self) -> dict[str, float]:
        """``billed - predicted`` for each rule, within this one replicate."""
        return {rule: self.billed_seconds - value for rule, value in self.predictions.items()}

    def matches(self, rule: str) -> bool:
        """Is this rule's prediction close enough to the bill to still be alive?"""
        return abs(self.misses[rule]) <= queries.MATCH_TOLERANCE_SECONDS


@dataclass(frozen=True)
class MeterCheck:
    """Whether the bills decode into whole seconds at the published rate.

    Every bill this experiment has ever collected was an exact whole number of
    seconds at the published rate. That makes it a strong check on the decoding:
    if the rate used here were wrong by even half a percent, the decoded bills
    would drift off the whole seconds and this would say so, before any of the
    multi-cluster numbers are read.
    """

    published: float
    size: str
    resource_constraint: str
    worst_gap_seconds: float
    worst_warehouse: str | None
    n: int
    ok: bool


def check_meter(
    observations: list[Observation],
    *,
    size: str,
    resource_constraint: str,
    tolerance: float = queries.WHOLE_SECOND_TOLERANCE,
) -> MeterCheck:
    """Confirm every decoded bill sits on a whole second."""
    published = queries.published_credits_per_hour(size, resource_constraint)
    worst_gap = 0.0
    worst_warehouse: str | None = None
    for observation in observations:
        gap = abs(observation.billed_seconds - round(observation.billed_seconds))
        if gap >= worst_gap:
            worst_gap, worst_warehouse = gap, observation.warehouse
    return MeterCheck(
        published=published,
        size=size.upper(),
        resource_constraint=resource_constraint.upper(),
        worst_gap_seconds=worst_gap,
        worst_warehouse=worst_warehouse,
        n=len(observations),
        ok=bool(observations) and worst_gap <= float(tolerance),
    )


@dataclass(frozen=True)
class MinimumCheck:
    """Whether `short` shows the 60-second minimum exists at all."""

    mean_billed: float
    mean_warehouse_seconds: float
    n: int
    holds: bool


# How far `short`'s mean bill may sit from 60 seconds and still count as showing
# the minimum. Generous, because this is a premise check rather than a
# measurement: it only has to separate "billed 60" from "billed 45".
MINIMUM_CHECK_SLACK_SECONDS = 5.0


def check_minimum(observations: list[Observation]) -> MinimumCheck:
    """Does a 45-second warehouse still bill 60 seconds?

    If it bills what it used, there is no minimum, the premise of the whole
    experiment is gone, and nothing else in the report means anything.
    """
    if not observations:
        return MinimumCheck(mean_billed=0.0, mean_warehouse_seconds=0.0, n=0, holds=False)
    mean_billed = statistics.fmean(o.billed_seconds for o in observations)
    mean_seconds = statistics.fmean(o.warehouse_seconds for o in observations)
    return MinimumCheck(
        mean_billed=mean_billed,
        mean_warehouse_seconds=mean_seconds,
        n=len(observations),
        holds=abs(mean_billed - queries.MINIMUM_SECONDS) <= MINIMUM_CHECK_SLACK_SECONDS,
    )


@dataclass(frozen=True)
class ScenarioResult:
    """One scenario's replicates, and which rules they leave standing."""

    name: str
    reads: str
    n: int
    #: How many clusters beyond the first this scenario actually got up.
    extra_clusters: int
    warehouse_seconds: float
    extra_seconds: float
    #: What the first cluster alone cost: a minute, or the time it ran.
    base_seconds: float
    billed_seconds: float
    extra_charge: float
    predicted_extra_charge: dict[str, float]
    #: How many of this scenario's replicates each rule predicted correctly.
    matched: dict[str, int] = field(default_factory=dict)

    @property
    def fits(self) -> list[str]:
        """Rules that predicted every one of this scenario's replicates."""
        return [rule for rule in RULES if self.matched.get(rule, 0) == self.n and self.n]

    @property
    def rules_out(self) -> list[str]:
        """Rules this scenario contradicts."""
        return [rule for rule in RULES if rule not in self.fits]


def _scenario_result(name: str, observations: list[Observation]) -> ScenarioResult:
    spec = queries.SCENARIOS_BY_NAME.get(name)
    return ScenarioResult(
        name=name,
        reads=spec.reads if spec else queries.READS_EXTRA,
        n=len(observations),
        extra_clusters=max(len(o.extras) for o in observations),
        warehouse_seconds=statistics.fmean(o.warehouse_seconds for o in observations),
        extra_seconds=statistics.fmean(sum(e.seconds for e in o.extras) for o in observations),
        base_seconds=statistics.fmean(o.base_seconds for o in observations),
        billed_seconds=statistics.fmean(o.billed_seconds for o in observations),
        extra_charge=statistics.fmean(o.extra_charge_seconds for o in observations),
        predicted_extra_charge={
            rule: statistics.fmean(o.predicted_extra_charge[rule] for o in observations) for rule in RULES
        },
        matched={rule: sum(1 for o in observations if o.matches(rule)) for rule in RULES},
    )


@dataclass(frozen=True)
class Verdict:
    """The answer, and the scenario-by-scenario evidence behind it."""

    outcome: str
    reason: str
    scenarios: list[ScenarioResult]
    fits: list[str]

    @property
    def answers(self) -> list[tuple[questions.Question, str]]:
        """Each question this experiment asks, answered from the rules that fit."""
        return [(question, questions.answer(question, self.fits)) for question in questions.QUESTIONS]


def deciding(results: list[ScenarioResult]) -> list[ScenarioResult]:
    """The scenarios a rule has to predict in order to survive.

    Every scenario except the cross-check. `natural` lets Snowflake scale out on
    its own, and it is reported in full — but its shape is whatever Snowflake
    chose that day rather than something this experiment set, so it confirms an
    answer instead of deciding one.
    """
    return [result for result in results if result.reads != queries.READS_CROSS_CHECK]


def compute_verdict(observations: list[Observation]) -> Verdict:
    """Which of the four rules predicts every replicate of every scenario.

    A rule survives a scenario when it predicts all of that scenario's
    replicates, and survives overall when it survives every scenario that
    decides. Exactly one survivor is the answer. Anything else is reported as
    what it is, with the numbers named, rather than rounded to the nearest rule.
    """
    if not observations:
        return Verdict(outcome=INCONCLUSIVE, reason="no replicates were measured", scenarios=[], fits=[])

    order = list(dict.fromkeys(o.scenario for o in observations))
    results = [_scenario_result(name, [o for o in observations if o.scenario == name]) for name in order]
    decide = deciding(results)
    if not decide:
        return Verdict(
            outcome=INCONCLUSIVE,
            reason="only the cross-check scenario produced usable replicates, and it is not built to decide",
            scenarios=results,
            fits=[],
        )

    fits = [rule for rule in RULES if all(rule in result.fits for result in decide)]

    if len(fits) == 1:
        rule = fits[0]
        return Verdict(
            outcome=rule,
            reason=f"every scenario was billed what this predicts: {RULE_TEXT[rule].says}",
            scenarios=results,
            fits=fits,
        )

    if not fits:
        return Verdict(
            outcome=NO_RULE_FITS,
            reason=(
                "no rule predicts the bill in every scenario, which points at something outside all four "
                "rather than a partial answer — start with the whole-second check on the credit rate"
            ),
            scenarios=results,
            fits=fits,
        )

    named = _listed([RULE_TEXT[rule].says for rule in fits])
    separator = _separating_scenario(decide, fits)
    designed = _designed_separator(fits)
    if separator:
        hint = f" — {separator} is the scenario that tells them apart, so check its replicates first"
    elif designed:
        hint = f" — {designed} is the scenario built to tell them apart, and it produced no usable replicates here"
    else:
        hint = ""
    return Verdict(
        outcome=INCONCLUSIVE,
        reason=f"{len(fits)} rules fit every scenario that ran ({named}), so this run does not separate them{hint}",
        scenarios=results,
        fits=fits,
    )


def _separating_scenario(results: list[ScenarioResult], fits: list[str]) -> str | None:
    """The scenario whose predictions for the surviving rules differ most.

    Named in an inconclusive verdict so the reader knows which measurement to
    look at rather than being told to look at all of them.
    """
    best_name, best_spread = None, 0.0
    for result in results:
        values = [result.predicted_extra_charge[rule] for rule in fits]
        spread = max(values) - min(values) if values else 0.0
        if spread > best_spread:
            best_name, best_spread = result.name, spread
    return best_name


def _spread_by_design(spec: queries.ScenarioSpec, rules: list[str]) -> float:
    """How far apart `rules` predictions are for one scenario as designed.

    Read off the nominal scenario table rather than off a run, so it answers
    "which scenario is supposed to separate these" even for a scenario that
    produced no replicates.
    """
    extras = [
        Extra(start_offset=float(spec.scale_out_at_seconds), seconds=float(spec.extra_cluster_seconds))
        for _ in range(spec.target_clusters - 1)
    ]
    values = [sum(charge(rule, extra) for extra in extras) for rule in rules]
    return max(values) - min(values) if values else 0.0


def _designed_separator(fits: list[str]) -> str | None:
    """The scenario the design relies on to tell `fits` apart, run or not.

    ``_separating_scenario`` can only speak for scenarios that produced
    replicates. When the one scenario that discriminates is exactly the one that
    failed, that leaves the reader with no name to chase, which is the moment
    they most need one.
    """
    best_name, best_spread = None, 0.0
    for spec in queries.MEASURED_SCENARIOS:
        spread = _spread_by_design(spec, fits)
        if spread > best_spread:
            best_name, best_spread = spec.name, spread
    return best_name


def _designed_separators(rules: list[str], *, other_than: str) -> list[str]:
    """Every other scenario that tells `rules` apart, widest margin first.

    A scenario that leaves several rules standing has not endorsed any of them;
    it has simply failed to separate them. Naming the scenarios that do separate
    them turns that into something the reader can go and check, instead of a
    list of claims sitting under the word "Conclusion".
    """
    apart = [
        (spec.name, _spread_by_design(spec, rules)) for spec in queries.MEASURED_SCENARIOS if spec.name != other_than
    ]
    return [name for name, spread in sorted(apart, key=lambda pair: -pair[1]) if spread > 0]


# --------------------------------------------------------------------------- #
# The same answer, in plain English
# --------------------------------------------------------------------------- #
# The numbers above say which rule survived. They do not say what that means for
# a bill, and the person who has to act on the result is not always the person
# who built the experiment. So everything below states it in words, and the words
# never use a constant name: a name that has to be looked up is not an
# explanation.


def _seconds(value: float) -> str:
    return f"{value:.1f}"


def scenario_conclusion(result: ScenarioResult) -> str:
    """What one scenario's numbers say, on their own.

    Written per scenario rather than only for the run as a whole, so a reader can
    follow the argument one measurement at a time: here is what it ran, here is
    what it was billed, and here is what that rules out.
    """
    if result.n == 0:
        return "No usable replicate, so this scenario says nothing."

    if result.reads == queries.READS_PREMISE:
        used, billed = _seconds(result.warehouse_seconds), _seconds(result.billed_seconds)
        if abs(result.billed_seconds - queries.MINIMUM_SECONDS) <= MINIMUM_CHECK_SLACK_SECONDS:
            return (
                f"The warehouse ran {used} seconds and was billed {billed}. The minimum is real on this "
                "account: about 15 seconds of every short run are paid for and unused, which is what makes "
                "the rest of these questions worth asking."
            )
        return (
            f"The warehouse ran {used} seconds and was billed {billed}, which is what it used. There is no "
            "60-second minimum on this account, so nothing else in this report is measuring what it claims to."
        )

    if result.reads == queries.READS_RATE:
        used, billed = _seconds(result.warehouse_seconds), _seconds(result.billed_seconds)
        gap = abs(result.billed_seconds - result.warehouse_seconds)
        if gap <= queries.MATCH_TOLERANCE_SECONDS:
            return (
                f"The warehouse ran {used} seconds and was billed {billed} — the same time, rounded up to the "
                "whole second. Credits are being turned into seconds correctly, so the multi-cluster numbers "
                "below can be read at face value."
            )
        return (
            f"The warehouse ran {used} seconds but was billed {billed}. One cluster past its minute should "
            "cost exactly the time it ran, so this gap is in the decoding, not in the billing, and every "
            "other number in this report is wrong by the same factor."
        )

    if result.extra_clusters == 1:
        lead = (
            f"The extra cluster ran {_seconds(result.extra_seconds)} seconds and added "
            f"{_seconds(result.extra_charge)} seconds to the bill."
        )
    else:
        lead = (
            f"The {result.extra_clusters} extra clusters ran {_seconds(result.extra_seconds)} seconds between "
            f"them and added {_seconds(result.extra_charge)} seconds to the bill."
        )
    if result.reads == queries.READS_CROSS_CHECK:
        agree = result.fits
        if not agree:
            tail = (
                " Snowflake started this cluster itself rather than being forced to, and none of the four ways "
                "of charging predicts what it cost. A cluster the account asked for and a cluster Snowflake "
                "chose to start are being billed differently, so the rest of this report does not carry over "
                "to a warehouse left to scale on its own."
            )
        else:
            tail = (
                " Snowflake started this cluster itself rather than being forced to, and it was billed the same "
                f"way: {_listed([RULE_TEXT[rule].says for rule in agree])}. Forcing a cluster up with "
                "MIN_CLUSTER_COUNT is not distorting the answer."
            )
        return lead + tail

    survived = result.fits
    killed = result.rules_out
    parts = [lead]
    if killed:
        rejected = _listed(
            [
                f"if {RULE_TEXT[rule].says}, it would have added {_seconds(result.predicted_extra_charge[rule])}"
                for rule in killed
            ]
        )
        parts.append(f"That rules out {len(killed)} of the four: {rejected}.")
    if len(survived) == 1:
        parts.append(f"What is left here: {RULE_TEXT[survived[0]].says}.")
    elif survived:
        # These are not findings. They are the rules this scenario failed to
        # separate, and saying so is the difference between a reader taking one
        # of them as the answer and going to look at the scenario that decides.
        agreeing = _listed([RULE_TEXT[rule].says for rule in survived])
        both = "both" if len(survived) == 2 else "all"
        parts.append(
            f"It cannot choose between the other {_counted(len(survived))}, which {both} predict that same "
            f"figure: {agreeing}."
        )
        elsewhere = _designed_separators(survived, other_than=result.name)
        if elsewhere:
            parts.append(f"Which of them holds is settled by {_named(elsewhere)}, not here.")
    else:
        parts.append("No rule predicted this, so the bill contains something none of the four accounts for.")
    return " ".join(parts)


def _evidence_paragraph(result: Verdict) -> str:
    if not result.scenarios:
        return "No scenario produced a usable replicate, so there is nothing here to read an answer from."
    rule = result.outcome if result.outcome in RULES else None
    parts = []
    for scenario in result.scenarios:
        billed = (
            f"{scenario.name}, over {scenario.n} replicate(s), was billed {_seconds(scenario.billed_seconds)} seconds"
        )
        if rule is None:
            parts.append(billed)
        else:
            predicted = scenario.base_seconds + scenario.predicted_extra_charge[rule]
            parts.append(f"{billed} against a prediction of {_seconds(predicted)}")
    lead = "What the bills actually were" if rule is None else "What the bills actually were, against that rule"
    return f"{lead}: " + "; ".join(parts) + "."


def _ruled_out_paragraph(result: Verdict) -> str:
    losers = [rule for rule in RULES if rule not in result.fits]
    if not losers or not result.scenarios:
        return ""
    parts = []
    for rule in losers:
        scenario = _worst_scenario(result, rule)
        if scenario is None:
            continue
        parts.append(
            f"if {RULE_TEXT[rule].says}, {scenario.name}'s extra clusters would have added "
            f"{_seconds(scenario.predicted_extra_charge[rule])} seconds to the bill, and they added "
            f"{_seconds(scenario.extra_charge)}"
        )
    if not parts:
        return ""
    return "Each of the other three is contradicted by a scenario that ran: " + "; ".join(parts) + "."


def _counted(n: int) -> str:
    """Small counts as words, because "the other 3" reads like a label."""
    return {2: "two", 3: "three", 4: "four"}.get(n, str(n))


def _named(names: list[str]) -> str:
    """Join scenario names. Commas, unlike :func:`_listed`: no clauses here."""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def _listed(items: list[str]) -> str:
    """Join whole clauses so a reader can see where one ends and the next begins.

    The rule sentences have commas inside them, so a comma-separated list would
    run them together. Semicolons keep the boundaries visible.
    """
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return "; ".join(items[:-1]) + "; and " + items[-1]


def _worst_scenario(result: Verdict, rule: str) -> ScenarioResult | None:
    """The scenario this rule gets most wrong."""
    scored = [
        (abs(scenario.predicted_extra_charge[rule] - scenario.extra_charge), scenario)
        for scenario in deciding(result.scenarios)
        if scenario.n
    ]
    if not scored:
        return None
    return max(scored, key=lambda pair: pair[0])[1]


def _closest_rule(result: Verdict) -> tuple[str | None, float]:
    """The rule that comes nearest to fitting, and its largest miss."""
    decide = [s for s in deciding(result.scenarios) if s.n]
    if not decide:
        return None, 0.0
    scored = [(max(abs(s.predicted_extra_charge[rule] - s.extra_charge) for s in decide), rule) for rule in RULES]
    miss, rule = min(scored, key=lambda pair: pair[0])
    return rule, miss


def _unsettled_paragraphs(result: Verdict) -> list[str]:
    if not result.scenarios:
        return ["This run measured nothing, so it cannot say anything about the minimum."]

    if not result.fits:
        rule, miss = _closest_rule(result)
        return [
            "None of the four ways of charging fits. Each mispredicts at least one scenario, and the closest "
            f"of them — {RULE_TEXT[rule].says} — is still off by {_seconds(miss)} seconds. A miss that size is "
            "not a near answer; it points at a term this experiment does not model at all. Check the "
            "whole-second test on the credit rate first: a rate that is wrong bends every bill by the same "
            "factor and produces exactly this shape."
        ]

    names = _listed([RULE_TEXT[rule].says for rule in result.fits])
    separator = _separating_scenario(deciding(result.scenarios), result.fits)
    designed = _designed_separator(result.fits)
    if separator:
        where = (
            f"The scenario that would separate them is {separator}; look at whether its replicates reached the "
            "cluster count they were meant to."
        )
    elif designed:
        where = (
            f"Nothing this run measured tells them apart. The scenario built to do it is {designed}, and it "
            "produced no usable replicates here, so re-run it before drawing a conclusion."
        )
    else:
        where = "No scenario in this run predicts different bills for them, so the run cannot separate them at all."
    return [
        f"This run cannot choose between {len(result.fits)} answers, each of which still fits every scenario "
        f"it ran: {names}. That is a limit of what this run measured, not a property of the billing.",
        where,
    ]


def explain(result: Verdict, *, minimum: MinimumCheck | None = None) -> list[str]:
    """The verdict as plain-English paragraphs, unwrapped, for the caller to fill.

    Returned rather than printed, so the CLI decides the width and the tests can
    read the words. Pass ``minimum`` to have the premise check spoken too: if the
    minimum does not exist on this account, that leads, because it makes every
    other paragraph moot.
    """
    paragraphs: list[str] = []

    if minimum is not None and not minimum.holds:
        paragraphs.append(
            "Read this first: the premise did not hold. The short scenario ran the warehouse for "
            f"{_seconds(minimum.mean_warehouse_seconds)} seconds and was billed {_seconds(minimum.mean_billed)} "
            "seconds, so there is no 60-second minimum on this account for a cluster to carry or share. "
            "Everything below is arithmetic resting on a premise this run just contradicted."
        )

    if result.outcome in RULES:
        rule = RULE_TEXT[result.outcome]
        paragraphs.append(f"The answer: {rule.says}.")
        paragraphs.append(f"What that means for a bill: {rule.means}")
    else:
        paragraphs.extend(_unsettled_paragraphs(result))

    paragraphs.append(_evidence_paragraph(result))
    ruled_out = _ruled_out_paragraph(result)
    if ruled_out:
        paragraphs.append(ruled_out)

    if minimum is not None and minimum.holds:
        paragraphs.append(
            f"The premise itself held: the short scenario used only {_seconds(minimum.mean_warehouse_seconds)} "
            f"seconds of warehouse time and was still billed {_seconds(minimum.mean_billed)} seconds. That is "
            "the 60-second minimum the whole question is about, and it is real on this account."
        )

    return paragraphs
