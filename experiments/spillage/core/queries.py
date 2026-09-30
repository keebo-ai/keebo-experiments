"""The SQL, workload, and scenarios behind the spillage demo.

Kept apart from the orchestration logic so the queries — the part you'd tweak to
change the workload or the reporting — read as data, in one place. No database
or CLI dependencies here.

The whole experiment turns on one idea: run the *same* workload on an
**undersized** warehouse (which runs out of memory and spills to disk) and on a
**right-sized** one (which does not), and compare the runtime and cost. Two
scenarios dial the memory pressure:

- ``local`` — the undersized warehouse spills to **local** SSD (slow).
- ``remote`` — a heavier workload pushes the undersized warehouse past local SSD
  into **remote** object storage (catastrophically slow).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from common import dedicated, warehouses

DEFAULT_WAREHOUSE = "SPILLAGE_DEMO_WH"
DEFAULT_DATABASE = "SPILLAGE_DEMO_DB"
OWNER_COMMENT = dedicated.owner_comment("spillage")
QUERY_TAG_PREFIX = "spill"

# --------------------------------------------------------------------------- #
# The source table
#
# `setup` generates its own table (once) instead of borrowing one, so the demo needs no
# sample-data share and behaves the same on every account. It has LINEITEM's
# column names and 60M rows (the size of TPCH_SF10). Every value is a hash of the
# row number, so the data is the same wherever it's generated.
# --------------------------------------------------------------------------- #
SOURCE_TABLE = "PUBLIC.LINEITEM"  # inside the demo database
SOURCE_ROWS = 60_000_000

SOURCE_TABLE_SQL = """
CREATE TABLE {table} AS
SELECT seq                                                           AS l_orderkey,
       ABS(HASH(seq, 1)) % 2000000 + 1                               AS l_partkey,
       ABS(HASH(seq, 2)) % 100000 + 1                                AS l_suppkey,
       ABS(HASH(seq, 3)) % 50 + 1                                    AS l_quantity,
       (ABS(HASH(seq, 4)) % 10400000 + 90000) / 100                  AS l_extendedprice,
       (ABS(HASH(seq, 5)) % 11) / 100                                AS l_discount,
       DATEADD('day', ABS(HASH(seq, 6)) % 2500, '1992-01-01'::DATE)  AS l_shipdate
FROM (SELECT SEQ8() AS seq FROM TABLE(GENERATOR(ROWCOUNT => {rows})))
"""

# --------------------------------------------------------------------------- #
# The spill-forcing workload
#
# One ROW_NUMBER() OVER (ORDER BY ...) with no PARTITION BY makes Snowflake sort
# every row in a single window. That sort is what overflows a small
# warehouse's memory and spills to disk. MAX(row_rank) keeps the result to one
# row while still needing every rank, so the sort can't be optimized away.
# Both warehouse sizes run the identical text, so any difference is the size.
#
# ``fanout`` cross-joins a tiny generated table to sort ``fanout`` copies of
# every row: fanout N sorts N x 60M rows. It is the one dial that sizes the
# demo. Too low and the undersized warehouse doesn't spill; too high and the
# right-sized one spills as well. ``run`` says which way to turn it.
# --------------------------------------------------------------------------- #
WORKLOAD_PREFIX = "SELECT MAX(row_rank) AS max_rank"

_WORKLOAD_SQL = """
SELECT MAX(row_rank) AS max_rank
FROM (
    SELECT ROW_NUMBER() OVER (
               ORDER BY l_extendedprice DESC, l_discount DESC, l_shipdate,
                        l_orderkey, l_partkey, l_suppkey{fan_order}
           ) AS row_rank
    FROM {table}{fan_join}
)
"""


def build_workload(table: str, fanout: int = 1) -> str:
    """The workload SQL over ``table``, sorting ``fanout`` copies of every row."""
    if fanout < 1:
        raise ValueError(f"fanout must be a positive integer, got {fanout}")
    fan_join = fan_order = ""
    if fanout > 1:
        fan_join = f"\n    CROSS JOIN (SELECT SEQ4() AS seq FROM TABLE(GENERATOR(ROWCOUNT => {int(fanout)}))) AS fan"
        fan_order = ", fan.seq"
    return _WORKLOAD_SQL.format(table=table, fan_join=fan_join, fan_order=fan_order).strip()


# --------------------------------------------------------------------------- #
# Scenarios
#
# Both compare X-Small (1 credit/hr) with Medium (4 credits/hr). Medium has
# roughly 4x the memory and local SSD, so there's a wide band of sort sizes that
# spill on X-Small and not on Medium. The fanout defaults are starting points:
# memory and local SSD per size vary by cloud and region, so calibrate once.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Scenario:
    """One demo: the same workload on an undersized vs. a right-sized warehouse."""

    name: str
    label: str
    fanout: int
    undersized: str  # warehouse size keyword
    right_sized: str  # warehouse size keyword
    blurb: str


SCENARIOS: dict[str, Scenario] = {
    "local": Scenario(
        name="local",
        label="Local spill",
        fanout=8,
        undersized="XSMALL",
        right_sized="MEDIUM",
        blurb=(
            "The X-Small runs out of memory and spills the sort to local SSD; the Medium, "
            "with 4x the memory, keeps it in memory. Same query, so the slowdown is the spill."
        ),
    ),
    "remote": Scenario(
        name="remote",
        label="Local + remote spill",
        fanout=40,
        undersized="XSMALL",
        right_sized="MEDIUM",
        blurb=(
            "A much larger sort overflows the X-Small's local SSD and spills to remote object "
            "storage (the performance cliff). The Medium, with 4x the local SSD, keeps it local."
        ),
    ),
}


def resolve_scenario(
    name: str,
    *,
    fanout: int | None = None,
    undersized: str | None = None,
    right_sized: str | None = None,
) -> Scenario:
    """Look up a scenario by name and apply any per-run overrides."""
    base = SCENARIOS.get(name.lower())
    if base is None:
        raise ValueError(f"unknown scenario {name!r}; choose one of: {', '.join(sorted(SCENARIOS))}")
    scenario = replace(
        base,
        fanout=fanout if fanout is not None else base.fanout,
        undersized=undersized.upper() if undersized else base.undersized,
        right_sized=right_sized.upper() if right_sized else base.right_sized,
    )
    if warehouses.credits_per_hour(scenario.undersized) >= warehouses.credits_per_hour(scenario.right_sized):
        raise ValueError(
            f"the undersized warehouse ({scenario.undersized}) must be smaller than the right-sized one "
            f"({scenario.right_sized})."
        )
    return scenario


# --------------------------------------------------------------------------- #
# The cost cap
#
# Each run gets a credit budget, split evenly between the two warehouse sizes
# and enforced as the warehouse's STATEMENT_TIMEOUT_IN_SECONDS: a size billing C
# credits/hr may run (budget / 2) * 3600 / C seconds. Snowflake cancels anything
# that runs longer, so the budget is a ceiling, not an estimate. The rate
# includes the warehouse's generation (Gen2 bills 1.35x).
# --------------------------------------------------------------------------- #
DEFAULT_MAX_CREDITS = 1.5
SETUP_TIMEOUT_SECONDS = 900  # generating the table: at most 0.25 credits on a Gen1 X-Small


def statement_timeout_s(size: str, *, generation: str, max_credits: float) -> int:
    """Seconds one query on ``size`` may run so it stays within half of ``max_credits``."""
    if max_credits <= 0:
        raise ValueError(f"max credits must be positive, got {max_credits}")
    seconds = int(max_credits / 2 * 3600 / warehouses.credits_per_hour(size, generation))
    if seconds < warehouses.BILLING_MINIMUM_SECONDS:
        raise ValueError(
            f"--max-credits {max_credits} leaves the {warehouses.SIZE_LABEL[size]} under "
            f"{warehouses.BILLING_MINIMUM_SECONDS} seconds (Snowflake bills that minimum anyway). "
            "Raise --max-credits."
        )
    return seconds


# --------------------------------------------------------------------------- #
# Near-real-time per-query stats
#
# QUERY_HISTORY_BY_SESSION returns this session's queries within seconds, where
# ACCOUNT_USAGE lags minutes, so the demo can show spill live. It's qualified
# with the demo database so it works on sessions with no current database.
# ``query_id`` is bound as a value (it contains hyphens).
# --------------------------------------------------------------------------- #
LIVE_STATS_SQL = """
SELECT bytes_spilled_to_local_storage  AS bytes_local,
       bytes_spilled_to_remote_storage AS bytes_remote,
       total_elapsed_time              AS elapsed_ms
FROM TABLE({database}.INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION(RESULT_LIMIT => 1000))
WHERE query_id = %s
"""

ACCOUNT_USAGE_PROBE = "SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY LIMIT 1"


# --------------------------------------------------------------------------- #
# Reconciliation report (ACCOUNT_USAGE)
#
# Read after the demo for the authoritative spill and BILLED credits. Every
# workload query is tagged ``spill:<run id>:<scenario>:<undersized|right_sized>``,
# so each run of each scenario is reported separately — rehearsals included.
# The placeholders are filled by ``report.read_report``: ``{wh}`` (upper-cased
# warehouse name), ``{hours}`` (lookback), ``{tag}`` (QUERY_TAG_PREFIX), and
# ``{workload}`` (WORKLOAD_PREFIX), so the filters can't drift from the run.
# --------------------------------------------------------------------------- #
REPORT_STEPS: list[tuple[int, str, str]] = [
    (
        1,
        "Runtime and spill per run (ACCOUNT_USAGE.QUERY_HISTORY)",
        """
SELECT SPLIT_PART(query_tag, ':', 2)                      AS run_id,
       SPLIT_PART(query_tag, ':', 3)                      AS scenario,
       REPLACE(SPLIT_PART(query_tag, ':', 4), '_', '-')   AS side,
       warehouse_size,
       ROUND(total_elapsed_time / 1000, 1)                AS runtime_s,
       ROUND(bytes_spilled_to_local_storage / POW(1024, 3), 2)  AS spill_local_gb,
       ROUND(bytes_spilled_to_remote_storage / POW(1024, 3), 2) AS spill_remote_gb,
       execution_status
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE warehouse_name = '{wh}'
  AND query_tag LIKE '{tag}:%'
  AND query_text ILIKE '{workload}%'
  AND start_time > DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
ORDER BY start_time
""",
    ),
    (
        2,
        "Billed credits per run (QUERY_ATTRIBUTION_HISTORY, can lag several hours)",
        """
SELECT SPLIT_PART(q.query_tag, ':', 2)                    AS run_id,
       SPLIT_PART(q.query_tag, ':', 3)                    AS scenario,
       REPLACE(SPLIT_PART(q.query_tag, ':', 4), '_', '-') AS side,
       q.warehouse_size,
       ROUND(SUM(a.credits_attributed_compute), 5)        AS billed_credits
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY a
JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q USING (query_id)
WHERE q.warehouse_name = '{wh}'
  AND q.query_tag LIKE '{tag}:%'
  AND q.query_text ILIKE '{workload}%'
  AND a.start_time > DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
GROUP BY 1, 2, 3, 4
ORDER BY 1, 2, 3
""",
    ),
    (
        3,
        "Everything the demo warehouse billed, setup and report included (WAREHOUSE_METERING_HISTORY)",
        """
SELECT SUM(credits_used)         AS total_billed_credits,
       SUM(credits_used_compute) AS compute_credits,
       MIN(start_time)           AS first_hour,
       MAX(end_time)             AS last_hour
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
WHERE warehouse_name = '{wh}'
  AND start_time > DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
""",
    ),
]
