"""The v1 run, decoded correctly — and what it already ruled out.

v1 reported INCONCLUSIVE with a billed figure of 618.3 cluster-seconds against
predictions of 657.1 and 807.1, and blamed a ~110-second per-cycle term it could
not explain. The warehouses were Generation 2 and the bills were decoded at the
Generation 1 rate, so every figure was 35% too large and the "unexplained
overhead" was the arithmetic error itself.

Decoded at 1.35 credits/hour instead, every v1 bill lands on a whole number of
seconds, and the four candidate rules in :mod:`verdict` disagree about it in a
way that leaves exactly one standing. That makes v1 a second, independent run
that agrees with v2, which is worth keeping.

Two things about the v1 data limit what can be read off it:

* The three ``treatment`` cycles shared one warehouse, so ACCOUNT_USAGE gave
  them one bill between them. That is exactly why v2 gives every cycle its own
  warehouse.
* v1 never recorded when an extra cluster started, only how long it lived. Every
  v1 extra cluster was suspended together with its warehouse, so its start
  offset is the warehouse's own runtime minus that lifetime — the arithmetic
  ``start_offset`` below. Each one started around the 77-80 second mark, past
  the warehouse's first minute in every case. So no v1 warehouse can say
  anything about a cluster started early, and it cannot tell one shared
  warehouse minute from no minimum at all — both charge the same once the
  warehouse's own minute is spent. v2 adds ``inside`` for exactly that.
"""

from __future__ import annotations

import pytest

from experiments.multi_cluster_billing.core import verdict
from experiments.multi_cluster_billing.core.questions import (
    OWN_MINUTE,
    PER_SECOND,
    SHARES_THE_MINUTE,
    WHOLE_MINUTES,
)

GEN2_XSMALL_RATE = 1.35
GEN1_XSMALL_RATE = 1.0

#: warehouse seconds, and the lifetime of each cluster beyond the first
TREATMENT_CYCLES = [
    (89.689, [12.548]),
    (89.533, [11.906]),
    (97.976, [18.483]),
]
#: One metering row covered all three treatment cycles.
TREATMENT_CREDITS = 0.171750
CALIBRATE = (0.121875, 201.516, [122.060])
NATURAL = (0.052500, 79.857, [2.385])


def extras_of(warehouse_seconds: float, lifetimes: list[float]) -> list[verdict.Extra]:
    """The extra clusters of one v1 cycle, with their start offsets reconstructed."""
    return [verdict.Extra(start_offset=warehouse_seconds - life, seconds=life) for life in lifetimes]


def predicted(rule: str, cycles: list[tuple[float, list[float]]]) -> float:
    """What `rule` says the bill for these cycles should have been, in seconds."""
    return sum(
        verdict.predict(rule, warehouse_seconds=warehouse_seconds, extras=extras_of(warehouse_seconds, lifetimes))
        for warehouse_seconds, lifetimes in cycles
    )


def test_the_gen1_rate_reproduces_the_number_v1_printed():
    assert verdict.cluster_seconds(TREATMENT_CREDITS, GEN1_XSMALL_RATE) == pytest.approx(618.3, abs=0.05)


def test_the_gen2_rate_decodes_every_v1_bill_to_a_whole_number_of_seconds():
    # The meter charges whole seconds. A rate that leaves fractions behind is the
    # wrong rate, which is how the v1 error would have been caught at the time.
    for credits in (TREATMENT_CREDITS, CALIBRATE[0], NATURAL[0]):
        seconds = verdict.cluster_seconds(credits, GEN2_XSMALL_RATE)
        assert seconds == pytest.approx(round(seconds), abs=0.01), credits
    assert verdict.cluster_seconds(TREATMENT_CREDITS, GEN2_XSMALL_RATE) == pytest.approx(458.0, abs=0.01)
    assert verdict.cluster_seconds(CALIBRATE[0], GEN2_XSMALL_RATE) == pytest.approx(325.0, abs=0.01)
    assert verdict.cluster_seconds(NATURAL[0], GEN2_XSMALL_RATE) == pytest.approx(140.0, abs=0.01)


def test_every_v1_extra_cluster_started_after_the_first_minute_was_over():
    # Which is why none of them can say anything about a cluster started early.
    # v2 adds `inside` for that.
    for warehouse_seconds, lifetimes in [*TREATMENT_CYCLES, CALIBRATE[1:], NATURAL[1:]]:
        for extra in extras_of(warehouse_seconds, lifetimes):
            assert extra.start_offset > 60.0


def test_the_treatment_bill_rules_out_both_ways_of_charging_without_a_per_cluster_minute():
    # Three clusters that lived 12, 12 and 18 seconds. Billed by the second they
    # would have added 43 seconds; each charged a full minute they add 180. The
    # bill is 136 seconds above the per-second figure and exact on the other.
    #
    # Every one of these clusters came up past the 60-second mark, so a warehouse
    # minute they could have shared was long spent and a shared minute charges
    # exactly what per-second does. Both die on the same 136 seconds.
    billed = verdict.cluster_seconds(TREATMENT_CREDITS, GEN2_XSMALL_RATE)
    assert billed == pytest.approx(458.0)
    assert predicted(PER_SECOND, TREATMENT_CYCLES) == pytest.approx(322.0)
    assert predicted(SHARES_THE_MINUTE, TREATMENT_CYCLES) == pytest.approx(322.0)
    assert predicted(OWN_MINUTE, TREATMENT_CYCLES) == pytest.approx(458.0)
    assert billed - predicted(PER_SECOND, TREATMENT_CYCLES) == pytest.approx(136.0)


def test_the_calibrate_bill_rules_out_charging_in_whole_minutes():
    # Its one extra cluster lived 122 seconds, so it is the only v1 warehouse
    # that says anything about a cluster past its first minute. Rounding up to
    # whole minutes would have charged 180 for it; charging the seconds it ran
    # charges 122. The bill matches the second of those.
    #
    # The other three rules all charge 122 here — a cluster that outlives a
    # minute on its own costs the same however the minimum is arranged — so this
    # warehouse separates whole minutes from everything else and nothing more.
    credits, warehouse_seconds, lifetimes = CALIBRATE
    billed = verdict.cluster_seconds(credits, GEN2_XSMALL_RATE)
    cycles = [(warehouse_seconds, lifetimes)]
    assert billed == pytest.approx(325.0)
    assert predicted(WHOLE_MINUTES, cycles) == pytest.approx(382.0)
    for rule in (PER_SECOND, OWN_MINUTE, SHARES_THE_MINUTE):
        assert predicted(rule, cycles) == pytest.approx(324.0), rule
    # Within the second the meter rounds by, so this one is a match, not a miss.
    assert billed - predicted(OWN_MINUTE, cycles) == pytest.approx(1.0)


def test_the_natural_warehouse_scaled_out_on_its_own_and_was_billed_the_same_way():
    # Nothing forced this one; the queries queued and Snowflake added the cluster
    # itself. Its extra cluster lived 2.4 seconds and cost a full minute.
    credits, warehouse_seconds, lifetimes = NATURAL
    cycles = [(warehouse_seconds, lifetimes)]
    billed = verdict.cluster_seconds(credits, GEN2_XSMALL_RATE)
    assert billed == pytest.approx(140.0)
    assert predicted(OWN_MINUTE, cycles) == pytest.approx(140.0)
    assert predicted(PER_SECOND, cycles) == pytest.approx(83.0)


def test_only_one_rule_survives_all_three_v1_warehouses():
    # Taken one at a time no v1 warehouse settles it. Taken together they do, and
    # they pick the same rule v2 picks, from a different account on a different
    # day. That agreement is the reason this file exists.
    bills = [
        (verdict.cluster_seconds(TREATMENT_CREDITS, GEN2_XSMALL_RATE), TREATMENT_CYCLES),
        (verdict.cluster_seconds(CALIBRATE[0], GEN2_XSMALL_RATE), [CALIBRATE[1:]]),
        (verdict.cluster_seconds(NATURAL[0], GEN2_XSMALL_RATE), [NATURAL[1:]]),
    ]
    fits = [
        rule for rule in verdict.RULES if all(abs(billed - predicted(rule, cycles)) <= 1.0 for billed, cycles in bills)
    ]
    assert fits == [OWN_MINUTE]


def test_no_unexplained_hundred_second_term_survives_the_correction():
    # v1's headline problem was a ~110-second per-cycle gap it could not account
    # for. Decoded at the right rate the largest gap is one second, which is the
    # second the meter rounds up to.
    gaps = [
        verdict.cluster_seconds(TREATMENT_CREDITS, GEN2_XSMALL_RATE) - predicted(OWN_MINUTE, TREATMENT_CYCLES),
        verdict.cluster_seconds(CALIBRATE[0], GEN2_XSMALL_RATE) - predicted(OWN_MINUTE, [CALIBRATE[1:]]),
        verdict.cluster_seconds(NATURAL[0], GEN2_XSMALL_RATE) - predicted(OWN_MINUTE, [NATURAL[1:]]),
    ]
    assert max(abs(gap) for gap in gaps) <= 1.0
