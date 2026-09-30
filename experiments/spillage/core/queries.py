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

from common.sql import validate_identifier

__all__ = [
    "CREDITS_PER_HOUR",
    "DEFAULT_MAX_CREDITS",
    "DEFAULT_TABLE",
    "DEFAULT_WAREHOUSE",
    "LIVE_STATS_SQL",
    "QUERY_TAG_PREFIX",
    "REPORT_STEPS",
    "SCENARIOS",
    "SIZES",
    "SIZE_KEYWORDS",
    "SIZE_LABEL",
    "Scenario",
    "build_workload",
    "statement_timeout_s",
    "resolve_scenario",
    "validate_identifier",
]


# --------------------------------------------------------------------------- #
# The spill-forcing workload
#
# A single ``ROW_NUMBER() OVER (ORDER BY ...)`` with no PARTITION forces
# Snowflake to sort *every* row of the table in one global window. That sort is
# what overflows a small warehouse's memory and spills to disk. Wrapping it in
# COUNT(*) keeps the result one row, so the sort — not data transfer — is the
# work. The query text is identical on both warehouse sizes, so any difference
# in runtime or spill is the size, not the query. ``IDENTIFIER($spill_table)``
# reads the table from a session variable the run sets.
#
# ``fanout`` cross-joins a tiny generated table to multiply the rows sorted: the
# workload sorts ``fanout`` x the table's rows (60M per step on TPCH_SF10). It is
# the one dial that sizes the demo. Too low and the undersized warehouse doesn't
# spill; too high and the right-sized one spills as well (and the run costs
# more). ``run`` prints which way to turn it if a scenario misses its target.
# --------------------------------------------------------------------------- #
def build_workload(fanout: int = 1) -> str:
    """Return the spill workload SQL, cross-joined ``fanout`` times to amplify it."""
    fanout = int(fanout)
    if fanout < 1:
        raise ValueError(f"fanout must be a positive integer, got {fanout}")
    join = ""
    extra_order = ""
    if fanout > 1:
        # A bounded, injection-free integer literal (validated above).
        join = f"\n            CROSS JOIN (SELECT SEQ4() AS seq FROM TABLE(GENERATOR(ROWCOUNT => {fanout}))) AS fan"
        extra_order = ", fan.seq"
    return (
        "SELECT COUNT(*) AS sorted_rows\n"
        "        FROM (\n"
        "            SELECT l_orderkey,\n"
        "                   ROW_NUMBER() OVER (\n"
        "                       ORDER BY l_extendedprice DESC, l_discount DESC, l_shipdate,\n"
        f"                                l_orderkey, l_partkey, l_suppkey{extra_order}\n"
        "                   ) AS row_rank\n"
        f"            FROM IDENTIFIER($spill_table){join}\n"
        "        )"
    )


# Each entry: (ALTER WAREHOUSE keyword, name recorded in QUERY_HISTORY, credits/hr).
SIZES: list[tuple[str, str, int]] = [
    ("XSMALL", "X-Small", 1),
    ("SMALL", "Small", 2),
    ("MEDIUM", "Medium", 4),
    ("LARGE", "Large", 8),
    ("XLARGE", "X-Large", 16),
    ("XXLARGE", "2X-Large", 32),
    ("XXXLARGE", "3X-Large", 64),
    ("X4LARGE", "4X-Large", 128),
]
SIZE_KEYWORDS = [keyword for keyword, _, _ in SIZES]
SIZE_LABEL = {keyword: label for keyword, label, _ in SIZES}
CREDITS_PER_HOUR = {keyword: credits for keyword, _, credits in SIZES}

DEFAULT_TABLE = "SNOWFLAKE_SAMPLE_DATA.TPCH_SF10.LINEITEM"  # 60M rows
DEFAULT_WAREHOUSE = "SPILLAGE_DEMO_WH"
QUERY_TAG_PREFIX = "spill"


@dataclass(frozen=True)
class Scenario:
    """One demo: the same workload on an undersized vs. a right-sized warehouse."""

    name: str
    label: str
    table: str
    fanout: int
    undersized: str  # ALTER WAREHOUSE size keyword
    right_sized: str  # ALTER WAREHOUSE size keyword
    blurb: str


# Both scenarios compare X-Small (1 credit/hr) with Medium (4 credits/hr).
# Medium has roughly 4x the memory and local SSD, so there's a wide band of sort
# sizes that spill on X-Small and not on Medium. The fanout defaults are
# starting points, not guarantees: memory and local SSD per size vary by cloud,
# region, and warehouse generation, so calibrate on your account.
SCENARIOS: dict[str, Scenario] = {
    "local": Scenario(
        name="local",
        label="Local spill",
        table=DEFAULT_TABLE,
        fanout=8,
        undersized="XSMALL",
        right_sized="MEDIUM",
        blurb=(
            "The X-Small runs out of memory and spills the sort to local SSD; the "
            "Medium, with 4x the memory, keeps it in memory. Same query, so the "
            "slowdown is the spill."
        ),
    ),
    "remote": Scenario(
        name="remote",
        label="Local + remote spill",
        table=DEFAULT_TABLE,
        fanout=40,
        undersized="XSMALL",
        right_sized="MEDIUM",
        blurb=(
            "A much larger sort overflows the X-Small's local SSD and spills to "
            "remote object storage (the performance cliff). The Medium, with 4x "
            "the local SSD, keeps its spill local."
        ),
    ),
}


# --------------------------------------------------------------------------- #
# The cost cap
#
# Each run gets a credit budget, split evenly between the two warehouse sizes.
# The budget becomes a warehouse STATEMENT_TIMEOUT: a size billing C credits/hr
# may run (budget / 2) * 3600 / C seconds, so the X-Small gets 4x the wall-clock
# time of the Medium for the same money. Snowflake cancels anything that runs
# past it, which makes the budget a ceiling rather than an estimate.
# --------------------------------------------------------------------------- #
DEFAULT_MAX_CREDITS = 1.5


def statement_timeout_s(size: str, *, max_credits: float, runs: int = 1) -> int:
    """Seconds one run on ``size`` may take so the arm stays within half of ``max_credits``."""
    if max_credits <= 0:
        raise ValueError(f"max credits must be positive, got {max_credits}")
    seconds = int(max_credits / 2 / runs * 3600 / CREDITS_PER_HOUR[size])
    if seconds < 60:
        raise ValueError(
            f"--max-credits {max_credits} leaves the {SIZE_LABEL[size]} under 60 seconds per run "
            "(Snowflake bills at least 60s anyway). Raise --max-credits or lower --runs."
        )
    return seconds


def resolve_scenario(
    name: str,
    *,
    table: str | None = None,
    fanout: int | None = None,
    undersized: str | None = None,
    right_sized: str | None = None,
) -> Scenario:
    """Look up a scenario by name and apply any per-run overrides."""
    base = SCENARIOS.get(name.lower())
    if base is None:
        known = ", ".join(sorted(SCENARIOS))
        raise ValueError(f"unknown scenario {name!r}; choose one of: {known}")
    return replace(
        base,
        table=table if table is not None else base.table,
        fanout=fanout if fanout is not None else base.fanout,
        undersized=undersized.upper() if undersized else base.undersized,
        right_sized=right_sized.upper() if right_sized else base.right_sized,
    )


# --------------------------------------------------------------------------- #
# Near-real-time per-query stats
#
# INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION returns this session's queries
# within seconds of completion — unlike ACCOUNT_USAGE, which lags minutes — so
# the demo can show spill live. ``query_id`` is bound as a value (it contains
# hyphens, so it is not a valid identifier to interpolate).
# --------------------------------------------------------------------------- #
LIVE_STATS_SQL = """
    SELECT bytes_spilled_to_local_storage  AS bytes_local,
           bytes_spilled_to_remote_storage AS bytes_remote,
           partitions_scanned              AS partitions_scanned,
           partitions_total                AS partitions_total,
           total_elapsed_time              AS total_elapsed_time
    FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION(RESULT_LIMIT => 1000))
    WHERE query_id = %s
"""


# --------------------------------------------------------------------------- #
# Reconciliation report (ACCOUNT_USAGE)
#
# Run after the demo to read the authoritative BILLED credits and spill back
# from Snowflake's own history. ``{hours}`` bounds the lookback; ``{wh}`` names
# the metering row. Both are validated (int / identifier) before formatting in.
# --------------------------------------------------------------------------- #
REPORT_STEPS: list[tuple[int, str, str]] = [
    (
        1,
        "Runtime + spill per run (from ACCOUNT_USAGE.QUERY_HISTORY)",
        """
        SELECT query_tag,
               warehouse_size,
               ROUND(total_elapsed_time / 1000, 1)                          AS elapsed_s,
               ROUND(bytes_spilled_to_local_storage  / POW(1024,3), 2)      AS gb_spill_local,
               ROUND(bytes_spilled_to_remote_storage / POW(1024,3), 2)      AS gb_spill_remote,
               partitions_scanned,
               partitions_total
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
        WHERE query_tag LIKE 'spill:%'
          AND query_text ILIKE 'SELECT COUNT(*) AS sorted_rows%'
          AND start_time > DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
        ORDER BY start_time
        """,
    ),
    (
        2,
        "Billed credits per warehouse size (QUERY_ATTRIBUTION_HISTORY — highest latency)",
        """
        SELECT q.warehouse_size,
               COUNT(*)                                    AS queries,
               ROUND(SUM(a.credits_attributed_compute), 5) AS billed_credits_total,
               ROUND(AVG(a.credits_attributed_compute), 5) AS billed_credits_per_query
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY a
        JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q USING (query_id)
        WHERE q.query_tag LIKE 'spill:%'
          AND a.start_time > DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
        GROUP BY q.warehouse_size
        ORDER BY billed_credits_total
        """,
    ),
    (
        3,
        "Authoritative billed total for the demo warehouse (WAREHOUSE_METERING_HISTORY)",
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
