"""The questions this experiment answers, and the billing rules it tells apart.

Prose and data only — no arithmetic, no I/O, no imports from :mod:`verdict`.
:mod:`verdict` imports this module and does the measuring, so the wording of a
question can change without touching the code that decides it.

There are two halves here:

* **The rules.** Four different ways Snowflake could charge for a cluster beyond
  the first. Each is a complete answer, and the scenarios in :mod:`queries` are
  chosen so that every pair of rules predicts a different bill in at least one
  of them. Exactly one rule should be left standing at the end of a run.
* **The questions.** What someone paying the bill actually wants to know, and
  what each rule would mean for them if it turned out to be the one. The answer
  to a question is not measured directly: the run decides which rule holds, and
  the rule decides the answer.
"""

from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# The candidate rules
#
# Each names a way of charging for ONE cluster beyond the first, given when that
# cluster started (S seconds after the warehouse started) and how long it ran
# (L seconds). `verdict.charge` turns each of these into a number.
# --------------------------------------------------------------------------- #

#: L. No minimum of its own; the seconds it ran are the seconds you pay for.
PER_SECOND = "PER_SECOND"

#: max(60, L). Its own minute, then by the second beyond that.
OWN_MINUTE = "OWN_MINUTE"

#: 60 * ceil(L / 60). Whole minutes, always rounded up.
WHOLE_MINUTES = "WHOLE_MINUTES"

#: max(0, min(L, S + L - 60)). The warehouse's first minute covers everything
#: running inside it, so a cluster is charged for the seconds it ran except any
#: that fell before the 60-second mark, and one that finishes before that mark
#: is free.
SHARES_THE_MINUTE = "SHARES_THE_MINUTE"

RULES: tuple[str, ...] = (PER_SECOND, OWN_MINUTE, WHOLE_MINUTES, SHARES_THE_MINUTE)


@dataclass(frozen=True)
class Rule:
    """One candidate way of charging for a cluster beyond the first."""

    name: str
    #: What the rule claims, in one sentence.
    says: str
    #: What it means for a bill you are trying to reduce.
    means: str


RULE_TEXT: dict[str, Rule] = {
    PER_SECOND: Rule(
        name=PER_SECOND,
        says="every cluster is charged for the seconds it ran, with no minimum of its own",
        means=(
            "Shortening any cluster by a second takes a second off the bill. There is no floor to run into "
            "and no boundary to round up to."
        ),
    ),
    OWN_MINUTE: Rule(
        name=OWN_MINUTE,
        says="every cluster is charged a full minute at the least, and by the second beyond that",
        means=(
            "A cluster that runs a minute or less costs a full minute no matter when it started or how "
            "briefly it ran. Past that minute you pay for the seconds, so length is worth controlling only "
            "once a cluster lives longer than a minute."
        ),
    ),
    WHOLE_MINUTES: Rule(
        name=WHOLE_MINUTES,
        says="every cluster is charged in whole minutes, rounded up",
        means=(
            "A cluster that runs 61 seconds costs the same as one that runs 120. The only length that saves "
            "money is one that drops the cluster below a whole-minute boundary."
        ),
    ),
    SHARES_THE_MINUTE: Rule(
        name=SHARES_THE_MINUTE,
        says=(
            "the warehouse's first minute covers every cluster running inside it, and a cluster is charged "
            "only for the part that runs past it"
        ),
        means=(
            "There is one minimum per warehouse, not one per cluster. A cluster that starts and finishes "
            "inside the warehouse's first minute costs nothing at all, so the cheapest moment to add capacity "
            "is as early as possible; past that minute you pay for the seconds and nothing more."
        ),
    ),
}


@dataclass(frozen=True)
class Question:
    """Something a person paying the bill wants to know.

    ``answer_if`` holds one plain-English answer per rule: what this question's
    answer becomes if that rule turns out to be the one Snowflake follows. It
    covers every rule, so the run can answer the question whichever way it goes.
    """

    key: str
    text: str
    #: Why the answer is worth a run of real compute.
    why: str
    #: The scenarios that decide it, by name.
    scenarios: tuple[str, ...]
    answer_if: dict[str, str]


SUSPEND_EARLY = Question(
    key="suspend_early",
    text="Does suspending an extra cluster before it has run a full minute save anything?",
    why=(
        "Scale-out is bursty: a cluster comes up for a spike of work and goes away again, often after only a "
        "few seconds. If Snowflake charges for the seconds it ran, then shutting it down 20 seconds sooner "
        "takes 20 seconds off the bill, and shortening bursts is worth doing. If instead the first minute is "
        "charged in full whatever happens, a cluster that ran 20 seconds and one that ran 55 cost exactly the "
        "same, and there is nothing to gain until a cluster is close to a full minute."
    ),
    scenarios=("brief", "nearly"),
    answer_if={
        PER_SECOND: ("Yes. Every second you take off a short cluster is a second off the bill."),
        OWN_MINUTE: (
            "No. A cluster that runs a minute or less costs a full minute, so suspending it sooner saves "
            "nothing. Only clusters that live past a minute are worth shortening."
        ),
        WHOLE_MINUTES: (
            "No. A cluster that runs a minute or less costs a full minute, so suspending it sooner saves "
            "nothing. Shortening only helps when it drops the cluster past a whole-minute boundary."
        ),
        SHARES_THE_MINUTE: (
            "Yes, for any cluster still running once the warehouse's first minute is over: past that point "
            "you pay by the second, so every second you take off is a second off the bill. A cluster that "
            "finishes inside that first minute was already free."
        ),
    },
)

START_EARLY = Question(
    key="start_early",
    text="Does it matter when an extra cluster starts?",
    why=(
        "A warehouse that runs 45 seconds is still charged for a full 60, so the last 15 of those seconds are "
        "paid for whether anything uses them or not. If a second cluster started inside that window is covered "
        "by the minute already paid, then adding capacity early is free and work should be arranged to use it. "
        "If instead each cluster's minute begins when that cluster begins, the clock is the same wherever it "
        "lands and the starting moment is not worth tuning."
    ),
    scenarios=("inside", "brief"),
    answer_if={
        PER_SECOND: ("Only through how long the cluster ends up running. The starting moment itself changes nothing."),
        OWN_MINUTE: (
            "No. Each cluster's minute begins when that cluster begins, so starting early costs the same as "
            "starting late."
        ),
        WHOLE_MINUTES: (
            "No. Each cluster's minute begins when that cluster begins, so starting early costs the same as "
            "starting late."
        ),
        SHARES_THE_MINUTE: (
            "Yes, a great deal. What you pay for is the part of a cluster that runs past the warehouse's "
            "first minute, so a cluster that starts and finishes inside that minute costs nothing and the "
            "same work is free early and paid for late."
        ),
    },
)

PAST_THE_MINUTE = Question(
    key="past_the_minute",
    text="What does an extra cluster that runs longer than a minute cost?",
    why=(
        "The questions above are about clusters that die before their first minute is up. Once a cluster is "
        "past that minute there are two plausible ways to charge for the rest: by the second, or by rounding "
        "up to the next whole minute. For a cluster that runs 90 seconds that is the difference between "
        "paying for 90 seconds and paying for 120 — a third more for the same work — and it decides whether a "
        "long-running cluster is worth controlling to the second or only to the minute."
    ),
    scenarios=("outlives",),
    answer_if={
        PER_SECOND: "By the second from the moment it starts. 90 seconds of cluster costs 90 seconds.",
        OWN_MINUTE: (
            "A full minute at the least, then by the second after that. 90 seconds of cluster costs 90 "
            "seconds; 30 seconds also costs 60."
        ),
        WHOLE_MINUTES: ("Rounded up to the next whole minute. 90 seconds of cluster costs 120, and so does 119."),
        SHARES_THE_MINUTE: (
            "By the second, except for any seconds that fell inside the warehouse's first minute, which was "
            "already paid for. A cluster that comes up at the 60-second mark and runs 90 seconds costs 90; one "
            "that comes up at the 30-second mark and runs the same 90 seconds costs 60, because 30 of its "
            "seconds were covered."
        ),
    },
)

WIDE_SCALE_OUT = Question(
    key="wide_scale_out",
    text="Does every extra cluster carry a minute of its own, or do clusters started together share one?",
    why=(
        "Scaling a warehouse from one cluster to five is a single decision, but it starts four clusters at "
        "once. If those four share one minimum between them, a wide scale-out is cheap: one minute covers all "
        "of it. If each carries its own minute, the same decision costs four minutes, and scaling out by four "
        "costs four times as much as scaling out by one. On a warehouse that scales out repeatedly through "
        "the day, that is the difference between a rounding error and the largest item on the bill."
    ),
    scenarios=("k5",),
    answer_if={
        PER_SECOND: (
            "Neither — there is no per-cluster minute to share. Each cluster is charged for the seconds it "
            "ran, so four short clusters cost four times one short cluster and nothing more."
        ),
        OWN_MINUTE: (
            "Each carries its own. Four clusters up for 20 seconds each cost four minutes, not one, so a wide "
            "scale-out has a floor that grows with its width."
        ),
        WHOLE_MINUTES: (
            "Each carries its own. Four clusters up for 20 seconds each cost four minutes, not one, so a wide "
            "scale-out has a floor that grows with its width."
        ),
        SHARES_THE_MINUTE: (
            "They share one, because there is only one minimum and the warehouse already paid it. Four "
            "clusters up for 20 seconds each cost 20 seconds apiece and no minute at all, so scaling out by "
            "four costs four times the seconds, not four times a minute."
        ),
    },
)

QUESTIONS: tuple[Question, ...] = (SUSPEND_EARLY, START_EARLY, PAST_THE_MINUTE, WIDE_SCALE_OUT)


def answer(question: Question, fits: list[str] | tuple[str, ...]) -> str:
    """Answer ``question`` given the rules the run could not rule out.

    One rule left is an answer. Several left is a shorter list than we started
    with, and saying which answers are still open is more use than saying
    nothing. None left means the bills follow something this experiment cannot
    express, and the honest report of that is not an answer at all.
    """
    remaining = [rule for rule in RULES if rule in fits]
    if len(remaining) == 1:
        return question.answer_if[remaining[0]]
    if not remaining:
        return (
            "Not answered. None of the four ways of charging that this experiment can express matches what "
            "the account was billed, so there is a term here nobody has accounted for."
        )
    options = " ".join(f"If {RULE_TEXT[rule].says}: {question.answer_if[rule]}" for rule in remaining)
    return f"Not settled by this run — {len(remaining)} answers are still possible. {options}"
