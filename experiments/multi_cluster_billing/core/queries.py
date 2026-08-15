"""The SQL, DDL and constants behind the multi-cluster billing test.

Kept apart from the orchestration so the parts you would tweak — the warehouse
settings, the scenario timings, the reporting queries — read as data, in one place.
No database or CLI dependencies here.

Every warehouse this experiment creates pins the same settings, because the
measurement is a warehouse's lifetime and anything Snowflake decides on its own
is a confound:

- ``AUTO_RESUME = FALSE`` stops a stray query from resuming a warehouse mid-scenario.
- ``AUTO_SUSPEND = 3600`` stops Snowflake from suspending during the idle phase.
- ``ENABLE_QUERY_ACCELERATION = FALSE`` keeps serverless compute out of the bill.
- ``SCALING_POLICY = STANDARD`` is the policy the controller will actually use.
"""

from __future__ import annotations

from dataclasses import dataclass

from common.sql import validate_identifier

# --------------------------------------------------------------------------- #
# Sizes and billing
#
# These are the published Generation 1 rates; a generation multiplier scales
# them. The published rate is what bills are decoded with, and the `control`
# scenario checks it rather than replacing it.
#
# v2 measured the rate from `control` instead, and that was a mistake: control
# ran 90.136 seconds and was billed 91, because the meter charges whole seconds
# and rounds up. Dividing 91 seconds of credits by 90.136 seconds of warehouse
# put the rate 0.5% high, which shrank every other bill by half a second and
# made every candidate rule miss by just enough to be rejected. The rounding is
# in the bill, not in the rate, so it belongs in the prediction.
# --------------------------------------------------------------------------- #
CREDITS_PER_HOUR: dict[str, float] = {
    "XSMALL": 1.0,
    "SMALL": 2.0,
    "MEDIUM": 4.0,
    "LARGE": 8.0,
    "XLARGE": 16.0,
    "XXLARGE": 32.0,
}
DEFAULT_SIZE = "XSMALL"

STANDARD_GEN_1 = "STANDARD_GEN_1"
STANDARD_GEN_2 = "STANDARD_GEN_2"
GEN2_MULTIPLIER = 1.35
DEFAULT_RESOURCE_CONSTRAINT = STANDARD_GEN_2
RESOURCE_CONSTRAINTS: dict[str, float] = {STANDARD_GEN_1: 1.0, STANDARD_GEN_2: GEN2_MULTIPLIER}

#: The two names for one property. ``RESOURCE_CONSTRAINT = STANDARD_GEN_N`` is
#: rejected outright on newer accounts ("Use the GENERATION property to set
#: warehouse hardware generation"), while ``GENERATION = 'N'`` is the documented
#: and recommended form everywhere, so it is the only one the DDL emits. The
#: STANDARD_GEN_N names stay internally: they key the rate table, the manifest
#: and the verdict.
GENERATION_OF: dict[str, str] = {STANDARD_GEN_1: "1", STANDARD_GEN_2: "2"}
RESOURCE_CONSTRAINT_OF: dict[str, str] = {digit: name for name, digit in GENERATION_OF.items()}


def resource_constraint_for_generation(generation: str) -> str | None:
    """``'2'`` -> ``'STANDARD_GEN_2'``; ``None`` for a generation we cannot map."""
    return RESOURCE_CONSTRAINT_OF.get(str(generation).strip().upper())


def published_credits_per_hour(size: str, resource_constraint: str) -> float:
    """The documented hourly rate for ``size`` on ``resource_constraint``."""
    rate = CREDITS_PER_HOUR.get(size.upper())
    if rate is None:
        raise ValueError(f"size must be one of {sorted(CREDITS_PER_HOUR)}, got {size!r}")
    multiplier = RESOURCE_CONSTRAINTS.get(resource_constraint.upper())
    if multiplier is None:
        raise ValueError(
            f"resource constraint must be one of {sorted(RESOURCE_CONSTRAINTS)}, got {resource_constraint!r}"
        )
    return rate * multiplier


# The minimum under test: a minute. What it attaches to — the warehouse, each
# cluster, or each scale-out — is the question.
MINIMUM_SECONDS = 60.0

# The meter bills whole seconds, rounded up. Every bill in the v2 run was an
# exact integer multiple of published_rate / 3600, with no exceptions across 18
# warehouses, so a prediction is compared with the bill only after rounding it
# up to the next whole second.
SECONDS_PER_HOUR = 3600.0

# How far a decoded bill may sit from a whole number of seconds before the
# report says the meter is not behaving the way the analysis assumes. Credits
# are published to nine decimal places, so a genuine whole second decodes to
# well within a thousandth.
WHOLE_SECOND_TOLERANCE = 0.02

# How far a bill may sit from a rule's prediction and still count as matching it.
# One second, because that is the size of the quantum the meter charges in: a
# lifetime read from an event log lands anywhere inside the second the meter
# rounded up to, so an exact match is not something a correct rule can promise.
MATCH_TOLERANCE_SECONDS = 1.0

# --------------------------------------------------------------------------- #
# Timings
#
# Every cycle suspends at a fixed offset from its resume, and starts its extra
# clusters at another fixed offset, so a replicate's shape does not depend on
# how long provisioning took. The two offsets per scenario are what the whole
# design turns on: `scale_out_at_seconds` decides when the extra cluster starts,
# and `cycle_seconds` decides when everything stops, so together they set how
# long the extra cluster lives and where in the warehouse's own minute it sat.
# See the scenario table below for what each combination is for.
# --------------------------------------------------------------------------- #
MAX_CLUSTER_COUNT = 5
DEFAULT_REPLICATES = 4

POLL_INTERVAL_SECONDS = 1.0
POLL_TIMEOUT_SECONDS = 180.0

# How far an event-derived lifetime may sit from the polled one before the report
# says so. A larger gap indicates a missed event or a bad pairing, not a result.
POLL_DISAGREEMENT_SECONDS = 5.0

# ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY: "latency for the view is up to 180
# minutes (3 hours)". A row also only appears once its hour has closed.
# WAREHOUSE_EVENTS_HISTORY carries the same documented lag.
METERING_LAG_HOURS = 3

WAREHOUSE_PREFIX = "KEEBO_MCB"
WAREHOUSE_COMMENT = "Keebo multi-cluster billing test - safe to drop"
DEFAULT_NATURAL_ROWCOUNT = 300_000_000


# What a scenario contributes to the answer.
READS_PREMISE = "premise"  # checks the 60-second minimum exists at all
READS_RATE = "rate"  # checks credits decode to seconds correctly
READS_EXTRA = "extra"  # measures what a cluster beyond the first costs
READS_CROSS_CHECK = "cross-check"  # confirms the answer outside forced conditions


@dataclass(frozen=True)
class ScenarioSpec:
    """One experimental condition: a warehouse configuration and a cycle recipe.

    Everything except the three numbers — ``target_clusters``,
    ``scale_out_at_seconds`` and ``cycle_seconds`` — is identical across the
    measured scenarios: same size, same generation, same MAX_CLUSTER_COUNT, same
    statements in the same order. A difference in the bill therefore has one
    candidate cause.

    ``does`` and ``why`` are the scenario's own account of itself: what it makes
    the warehouse do, and which billing question the numbers it produces can
    settle. The report prints them beside the measurement, so a run explains
    itself to someone who has never read this file.

    ``kind`` is ``"forced"`` (raise MIN_CLUSTER_COUNT on a schedule) or
    ``"natural"`` (let queued queries make Snowflake scale out on its own).
    """

    name: str
    target_clusters: int
    #: Offset from the resume at which MIN_CLUSTER_COUNT is raised.
    scale_out_at_seconds: int
    #: Offset from the resume at which the warehouse is suspended.
    cycle_seconds: int
    max_concurrency_level: int | None
    kind: str
    reads: str
    does: str
    why: str

    @property
    def extra_cluster_seconds(self) -> int:
        """How long each extra cluster is meant to live.

        The design figure, not a measurement: the real lifetime comes from
        WAREHOUSE_EVENTS_HISTORY and lands a second or two either side of this.
        """
        return 0 if self.target_clusters < 2 else self.cycle_seconds - self.scale_out_at_seconds


SHORT = ScenarioSpec(
    name="short",
    target_clusters=1,
    scale_out_at_seconds=25,
    cycle_seconds=45,
    max_concurrency_level=None,
    kind="forced",
    reads=READS_PREMISE,
    does="Runs one cluster for 45 seconds, then suspends.",
    why=(
        "Everything else here rests on the 60-second minimum being real. A warehouse that ran for 45 seconds "
        "should still be billed for 60. If it is billed for 45 there is no minimum on this account, and none "
        "of the other questions mean anything."
    ),
)

CONTROL = ScenarioSpec(
    name="control",
    target_clusters=1,
    scale_out_at_seconds=70,
    cycle_seconds=90,
    max_concurrency_level=None,
    kind="forced",
    reads=READS_RATE,
    does="Runs one cluster for 90 seconds, then suspends.",
    why=(
        "One cluster, comfortably past its minute, so the bill should be nothing but the time it ran, at the "
        "published rate. It is the check that credits are being turned into seconds correctly before any of "
        "the multi-cluster numbers are read. If this one decodes wrong, every other number is wrong by the "
        "same factor."
    ),
)

INSIDE = ScenarioSpec(
    name="inside",
    target_clusters=2,
    scale_out_at_seconds=10,
    cycle_seconds=45,
    max_concurrency_level=None,
    kind="forced",
    reads=READS_EXTRA,
    does=(
        "Starts a second cluster 10 seconds in, then suspends the whole warehouse at 45 seconds. The second "
        "cluster lives about 35 seconds, and both clusters are gone before the warehouse's first minute is up."
    ),
    why=(
        "A warehouse that runs 45 seconds is still charged for a full 60, so 15 of those seconds are paid for "
        "and unused. The question is whether a second cluster started during that time is covered by the "
        "minute already paid for, or charged a full minute of its own on top of it. If it is covered, the "
        "second cluster adds nothing to the bill. If it carries its own minute, it adds 60 seconds. If it is "
        "simply charged for the 35 seconds it ran, it adds 35. Those three numbers are far enough apart that "
        "one run separates them."
    ),
)

BRIEF = ScenarioSpec(
    name="brief",
    target_clusters=2,
    scale_out_at_seconds=70,
    cycle_seconds=90,
    max_concurrency_level=None,
    kind="forced",
    reads=READS_EXTRA,
    does=(
        "Runs one cluster for 70 seconds, starts a second, then suspends 20 seconds later. The second cluster "
        "lives about 20 seconds, and the warehouse is well past its own first minute before that cluster "
        "starts."
    ),
    why=(
        "This is the ordinary shape of a scale-out: a burst of work arrives, a cluster comes up for a few "
        "seconds, the burst passes. The warehouse's own minute is already spent by then, so whatever this "
        "cluster adds to the bill is its own. If it adds about 20 seconds, short clusters are charged for the "
        "seconds they run. If it adds 60, the first minute of a cluster is a flat fee and there is no such "
        "thing as a cheap 20-second cluster."
    ),
)

NEARLY = ScenarioSpec(
    name="nearly",
    target_clusters=2,
    scale_out_at_seconds=65,
    cycle_seconds=120,
    max_concurrency_level=None,
    kind="forced",
    reads=READS_EXTRA,
    does=(
        "The same shape as `brief`, but the warehouse runs 120 seconds and the second cluster starts at 65, "
        "so it lives about 55 seconds — just under a full minute."
    ),
    why=(
        "Read next to `brief`, this says whether the first minute is a flat fee or not. If the extra cluster "
        "in `brief` adds about 20 seconds and this one adds about 55, Snowflake is charging by the second and "
        "every second taken off a cluster is money saved. If both add exactly 60, the first minute is flat: a "
        "20-second cluster and a 55-second cluster cost the same, and suspending either one sooner saves "
        "nothing at all."
    ),
)

OUTLIVES = ScenarioSpec(
    name="outlives",
    target_clusters=2,
    scale_out_at_seconds=60,
    cycle_seconds=150,
    max_concurrency_level=None,
    kind="forced",
    reads=READS_EXTRA,
    does=(
        "Runs the warehouse for 150 seconds and starts a second cluster at 60, so that cluster lives about 90 "
        "seconds — a minute and a half, well past its own minimum."
    ),
    why=(
        "Every other scenario here has extra clusters that die before their first minute is up, so none of "
        "them can say what happens afterwards. If the time past the first minute is charged by the second, "
        "this cluster adds about 90 seconds to the bill. If Snowflake rounds up to the next whole minute, it "
        "adds 120 — a third more for the same work. That is what decides whether a long-running cluster is "
        "worth controlling to the second or only to the minute."
    ),
)

K5 = ScenarioSpec(
    name="k5",
    target_clusters=5,
    scale_out_at_seconds=70,
    cycle_seconds=90,
    max_concurrency_level=None,
    kind="forced",
    reads=READS_EXTRA,
    does=(
        "Runs one cluster for 70 seconds, then forces the warehouse to five clusters and suspends 20 seconds "
        "later. Four extra clusters come up at the same moment and each lives about 20 seconds."
    ),
    why=(
        "Scaling from one cluster to five is one decision, but it starts four clusters. If the minimum is "
        "charged once per scale-out, the four together add about 60 seconds to the bill. If each cluster "
        "carries a minute of its own, they add 240 — four times as much for the same decision. On a warehouse "
        "that scales out repeatedly through the day, that is the difference between a rounding error and the "
        "largest item on the bill."
    ),
)

NATURAL = ScenarioSpec(
    name="natural",
    target_clusters=2,
    scale_out_at_seconds=0,
    cycle_seconds=0,
    max_concurrency_level=1,
    kind="natural",
    reads=READS_CROSS_CHECK,
    does=(
        "Sends two competing queries at a warehouse that allows one query per cluster, then waits for them, "
        "so Snowflake decides on its own to start a second cluster. Nothing here is on a fixed clock."
    ),
    why=(
        "Every other scenario forces a cluster up by raising MIN_CLUSTER_COUNT, which is not how a real "
        "warehouse scales. This one lets Snowflake scale out for the usual reason — a query waiting in a "
        "queue — and checks that the bill follows the same rule. If a cluster Snowflake started for itself "
        "were charged differently from one we forced, none of the other answers would carry over to "
        "production. It does not decide the verdict; it is a check on it."
    ),
)

# Order matters: the run walks this list as a Latin square, and the report
# prints scenarios in this order. Premise and rate first, then the four
# scenarios that measure what an extra cluster costs.
MEASURED_SCENARIOS: list[ScenarioSpec] = [SHORT, CONTROL, INSIDE, BRIEF, NEARLY, OUTLIVES, K5]
ALL_SCENARIOS: list[ScenarioSpec] = [*MEASURED_SCENARIOS, NATURAL]
SCENARIOS_BY_NAME: dict[str, ScenarioSpec] = {spec.name: spec for spec in ALL_SCENARIOS}

#: Scenarios whose extra clusters carry the answer. `short` and `control` check
#: the premise and the rate; these are what tell the four rules apart.
EXTRA_SCENARIOS: list[ScenarioSpec] = [spec for spec in MEASURED_SCENARIOS if spec.reads == READS_EXTRA]


def warehouse_name(prefix: str, scenario: str, replicate: int, run_token: str) -> str:
    """Build this run's warehouse name for one replicate of ``scenario``.

    One warehouse per replicate, so one metering row per replicate: three cycles
    inside a shared warehouse sum into a single row, and no interval can be
    computed from a single number.
    """
    validate_identifier(prefix, "warehouse prefix")
    validate_identifier(run_token, "run token")
    validate_identifier(scenario, "scenario")
    if int(replicate) < 1:
        raise ValueError(f"replicate must be 1 or greater, got {replicate!r}")
    return f"{prefix}_{scenario}_R{int(replicate)}_{run_token}".upper()


def create_warehouse_sql(name: str, *, size: str, spec: ScenarioSpec, resource_constraint: str) -> str:
    """Build the ``CREATE WAREHOUSE`` for one replicate.

    The generation is pinned rather than inherited from the account default. v1
    inherited it, which is how a Gen2 warehouse came to be decoded at the Gen1
    rate. It is pinned with ``GENERATION``, not ``RESOURCE_CONSTRAINT``: the two
    are equivalent, but newer accounts reject the latter outright.

    ``MAX_CLUSTER_COUNT`` is the same for every scenario, including the k=1 ones.
    v1 set it per scenario, so the scenarios being compared differed in two ways
    rather than one.
    """
    validate_identifier(name, "warehouse")
    # Raises on a bad size or generation before any SQL is built.
    published_credits_per_hour(size, resource_constraint)

    settings = [
        f"WAREHOUSE_SIZE = {size.upper()}",
        "WAREHOUSE_TYPE = STANDARD",
        f"GENERATION = '{GENERATION_OF[resource_constraint.upper()]}'",
        "SCALING_POLICY = STANDARD",
        "MIN_CLUSTER_COUNT = 1",
        f"MAX_CLUSTER_COUNT = {MAX_CLUSTER_COUNT}",
        "AUTO_SUSPEND = 3600",
        "AUTO_RESUME = FALSE",
        "INITIALLY_SUSPENDED = TRUE",
        "ENABLE_QUERY_ACCELERATION = FALSE",
    ]
    if spec.max_concurrency_level is not None:
        settings.append(f"MAX_CONCURRENCY_LEVEL = {int(spec.max_concurrency_level)}")
    settings.append(f"COMMENT = '{WAREHOUSE_COMMENT}'")
    return f"CREATE WAREHOUSE {name} " + " ".join(settings)


def natural_query(rowcount: int) -> str:
    """A query that occupies a cluster for a while and cannot be served from cache.

    ``RANDOM()`` makes it non-deterministic, so a repeat run re-executes instead
    of returning the cached result for free. ``GENERATOR`` keeps the experiment
    self-contained — no sample-data share to mount.
    """
    if int(rowcount) <= 0:
        raise ValueError(f"rowcount must be positive, got {rowcount!r}")
    return (
        "SELECT COUNT(*) FROM ("
        "SELECT SEQ8() AS s, RANDOM() AS r "
        f"FROM TABLE(GENERATOR(ROWCOUNT => {int(rowcount)}))"
        ") WHERE r < 0"
    )


# --------------------------------------------------------------------------- #
# Statements issued during a run
# --------------------------------------------------------------------------- #
SHOW_WAREHOUSE = "SHOW WAREHOUSES LIKE '{wh}'"
RESUME = "ALTER WAREHOUSE {wh} RESUME"
SUSPEND = "ALTER WAREHOUSE {wh} SUSPEND"
SET_MIN_CLUSTERS = "ALTER WAREHOUSE {wh} SET MIN_CLUSTER_COUNT = {n}"
DROP = "DROP WAREHOUSE IF EXISTS {wh}"
USE = "USE WAREHOUSE {wh}"
SESSION_INFO = "SELECT CURRENT_ACCOUNT(), CURRENT_REGION(), CURRENT_VERSION(), CURRENT_WAREHOUSE()"
ACCOUNT_USAGE_PROBE = "SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY LIMIT 1"
SHOW_RESOURCE_MONITORS = "SHOW RESOURCE MONITORS"

# --------------------------------------------------------------------------- #
# Reporting
#
# `credits_used_compute`, never `credits_used`: the ALTERs and the polling
# generate cloud-services credits that have nothing to do with cluster lifetime,
# and CREDITS_USED_CLOUD_SERVICES lags six hours against the view's three, so
# the sum reads incomplete at the point this report becomes runnable.
# --------------------------------------------------------------------------- #
METERING_SQL = """
SELECT warehouse_name,
       SUM(credits_used_compute)        AS compute_credits,
       SUM(credits_used_cloud_services) AS cloud_services_credits,
       MIN(start_time)                  AS first_hour,
       MAX(end_time)                    AS last_hour,
       COUNT(*)                         AS hourly_rows
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
WHERE warehouse_name IN ({names})
  AND end_time   > TO_TIMESTAMP_TZ('{window_start}')
  AND start_time < TO_TIMESTAMP_TZ('{window_end}')
GROUP BY warehouse_name
ORDER BY warehouse_name
"""

# --------------------------------------------------------------------------- #
# Event ordering
#
# ACCOUNT_USAGE returns rows sharing a timestamp in no fixed order. The v1 run
# produced `SPINUP_CLUSTER, ALTER_WAREHOUSE` at 05:57:47.618 and
# `ALTER_WAREHOUSE, SPINUP_CLUSTER` at 05:59:19.355 — the same pair, opposite
# order, same run — and interleaved a WAREHOUSE_CONSISTENT between two
# SUSPEND_CLUSTER rows. Pairing each completion marker with the transition it
# completes needs a total order, so one is imposed here.
#
# WAREHOUSE_CONSISTENT sorts last within a timestamp because it is the row that
# says the transition finished; the others may be the row that says it started.
# Unknown events sort between the cluster events and the marker, so a new event
# name never displaces the marker.
# --------------------------------------------------------------------------- #
EVENT_PHASE_RANK: dict[str, int] = {
    "CREATE_WAREHOUSE": 0,
    "RESUME_WAREHOUSE": 1,
    "SUSPEND_WAREHOUSE": 1,
    "ALTER_WAREHOUSE": 2,
    "SPINUP_CLUSTER": 3,
    "RESUME_CLUSTER": 4,
    "SUSPEND_CLUSTER": 4,
    "WAREHOUSE_CONSISTENT": 9,
}
UNKNOWN_PHASE_RANK = 5


def phase_rank_case(column: str) -> str:
    """The ordering expression for ``column``, built from EVENT_PHASE_RANK."""
    whens = " ".join(f"WHEN '{name}' THEN {rank}" for name, rank in EVENT_PHASE_RANK.items())
    return f"CASE {column} {whens} ELSE {UNKNOWN_PHASE_RANK} END"


EVENTS_SQL = f"""
SELECT warehouse_name, cluster_number, event_name, event_reason, event_state, timestamp
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_EVENTS_HISTORY
WHERE warehouse_name IN ({{names}})
  AND timestamp BETWEEN TO_TIMESTAMP_TZ('{{window_start}}') AND TO_TIMESTAMP_TZ('{{window_end}}')
ORDER BY warehouse_name,
         timestamp,
         {phase_rank_case("event_name")},
         cluster_number NULLS FIRST
"""

NATURAL_QUERIES_SQL = """
SELECT query_id,
       cluster_number,
       ROUND(queued_overload_time / 1000, 2) AS queued_overload_s,
       ROUND(total_elapsed_time / 1000, 2)   AS elapsed_s,
       start_time
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE query_id IN ({ids})
ORDER BY start_time
"""
