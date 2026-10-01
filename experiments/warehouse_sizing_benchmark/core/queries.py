"""The SQL and constants behind the warehouse-sizing benchmark.

Kept apart from the orchestration logic so the queries, the part you'd tweak to
change the workload or the reporting, read as data in one place. No database or
CLI dependencies here.
"""

from __future__ import annotations

import re

from common import warehouses

# --------------------------------------------------------------------------- #
# The benchmark workload
#
# This SELECT is identical on every run at every size, so any difference in
# timing is the warehouse, not the query. ``IDENTIFIER($lineitem_table)`` reads
# the table from a session variable set by the sweep (Step 2 of the article).
# Grouping ~600M rows by (order, supplier) gives almost one group per row, and
# that hash table is what spills to disk on small warehouses.
# --------------------------------------------------------------------------- #
BENCHMARK_QUERY = (
    "SELECT l_orderkey, l_suppkey, COUNT(*) AS line_count, "
    "SUM(l_quantity) AS total_qty, "
    "SUM(l_extendedprice * (1 - l_discount)) AS net_revenue, "
    "AVG(l_discount) AS avg_discount "
    "FROM IDENTIFIER($lineitem_table) "
    "GROUP BY l_orderkey, l_suppkey "
    "ORDER BY net_revenue DESC LIMIT 100"
)

DEFAULT_TABLE = "SNOWFLAKE_SAMPLE_DATA.TPCH_SF100.LINEITEM"
DEFAULT_WAREHOUSE = "SIZING_BENCHMARK_WH"
DEFAULT_DATABASE = "SIZING_BENCHMARK_DB"

# Marks the warehouse and database as the benchmark's own. Nothing is resized,
# suspended, or dropped without it. It's the comment earlier versions put on
# the warehouse, so a warehouse they created is still recognised.
OWNER_COMMENT = "Keebo warehouse-sizing benchmark - safe to drop"

# Every benchmark query is tagged wsbench:<run id>:<size>:<attempt>. Run ids are
# UTC timestamps (e.g. 20261001-143318), so the latest run sorts last.
QUERY_TAG_PREFIX = "wsbench"
_RUN_ID = re.compile(r"[0-9A-Za-z-]+")


def validate_run_id(run_id: str) -> str:
    """Return ``run_id`` if it's safe inside a query tag, else raise ``ValueError``."""
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError(f"run id must be letters, digits, and dashes, got {run_id!r}")
    return run_id


# --------------------------------------------------------------------------- #
# The generated fallback table
#
# Used only when the role can't read the SNOWFLAKE_SAMPLE_DATA share. `setup`
# then generates TPCH_SF100's row count in the benchmark's own database, with
# the LINEITEM columns and types the benchmark reads. Every value is a hash of
# the row number, and order keys repeat about four times, as in TPC-H, so the
# GROUP BY has the same shape.
# --------------------------------------------------------------------------- #
GENERATED_TABLE = "PUBLIC.LINEITEM"  # inside the benchmark database
SOURCE_ROWS = 600_000_000
IDLE_TIMEOUT_SECONDS = 1800  # the warehouse's timeout between runs: setup's checks and `report`
GENERATE_SIZE = "MEDIUM"  # 4x the X-Small's speed for the same credits
GENERATE_TIMEOUT_SECONDS = 450  # generating the table: at most 0.5 credits on a Gen1 Medium

GENERATED_TABLE_SQL = """
CREATE TABLE {table} AS
SELECT FLOOR(seq / 4) + 1                                              AS l_orderkey,
       ABS(HASH(seq, 1)) % 20000000 + 1                                AS l_partkey,
       ABS(HASH(seq, 2)) % 1000000 + 1                                 AS l_suppkey,
       (ABS(HASH(seq, 3)) % 50 + 1)::NUMBER(12, 2)                     AS l_quantity,
       ((ABS(HASH(seq, 4)) % 10400000 + 90000) / 100)::NUMBER(12, 2)   AS l_extendedprice,
       ((ABS(HASH(seq, 5)) % 11) / 100)::NUMBER(12, 2)                 AS l_discount,
       DATEADD('day', ABS(HASH(seq, 6)) % 2500, '1992-01-01'::DATE)    AS l_shipdate
FROM (SELECT SEQ8() AS seq FROM TABLE(GENERATOR(ROWCOUNT => {rows})))
"""

# --------------------------------------------------------------------------- #
# The cost cap (see sweep.query_timeouts)
# --------------------------------------------------------------------------- #
DEFAULT_MAX_CREDITS = 3.0
OVERHEAD_SECONDS_PER_QUERY = 10  # the tag ALTERs and stats lookups billed alongside each query
MIN_TIMEOUT_SECONDS = 10

# --------------------------------------------------------------------------- #
# Live per-query stats
#
# ACCOUNT_USAGE lags, so `run` reads two faster sources right after each query:
#
# - GET_QUERY_OPERATOR_STATS: each operator's spill, as soon as the query
#   finishes. An operator that didn't spill has no ``spilling`` entry (0).
# - QUERY_HISTORY_BY_SESSION: the query's elapsed time, qualified with the
#   benchmark database so it works on sessions with no current database.
#
# ``query_id`` is bound as a value because it contains hyphens.
# --------------------------------------------------------------------------- #
SPILL_SQL = """
SELECT COUNT(*)                                                                         AS operators,
       COALESCE(SUM(operator_statistics:spilling:bytes_spilled_local_storage::NUMBER), 0)  AS bytes_local,
       COALESCE(SUM(operator_statistics:spilling:bytes_spilled_remote_storage::NUMBER), 0) AS bytes_remote
FROM TABLE(GET_QUERY_OPERATOR_STATS(%s))
"""

ELAPSED_SQL = """
SELECT total_elapsed_time AS elapsed_ms
FROM TABLE({database}.INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION(RESULT_LIMIT => 1000))
WHERE query_id = %s
"""

ACCOUNT_USAGE_PROBE = "SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY LIMIT 1"

# --------------------------------------------------------------------------- #
# Reporting queries (Steps 10-16)
#
# The report reads one run: the latest by default. Fill the placeholders with
# report_sql(): ``{wh}`` is the upper-cased warehouse name, ``{tag}`` the run's
# tag prefix, ``{hours}`` the lookback, ``{multiplier}`` the generation's rate
# multiple, and ``{rates}`` / ``{size_order}`` come from the shared size table.
#
# How long each view takes to catch up: QUERY_HISTORY up to ~45 minutes,
# WAREHOUSE_METERING_HISTORY up to ~3 hours, QUERY_ATTRIBUTION_HISTORY up to ~8.
# --------------------------------------------------------------------------- #
LATEST_RUN_SQL = """
SELECT MAX(SPLIT_PART(query_tag, ':', 2)) AS run_id
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE warehouse_name = '{wh}'
  AND query_tag LIKE 'wsbench:%'
  -- Only timestamp run ids; tags from before run ids (wsbench:<size>:<attempt>) don't match.
  AND REGEXP_LIKE(SPLIT_PART(query_tag, ':', 2), '[0-9]{{8}}-[0-9]{{6}}')
  AND start_time > DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
"""

# One row per size, e.g. SELECT 'X-Small' sz, 1 cph, 1 ord UNION ALL ...
_RATES = "\n            UNION ALL ".join(
    f"SELECT '{label}' sz, {credits} cph, {order} ord" for order, (_, label, credits) in enumerate(warehouses.SIZES, 1)
)
# The sizes in order, for ORDER BY CASE <size column> {size_order} END.
_SIZE_ORDER = " ".join(f"WHEN '{label}' THEN {order}" for order, (_, label, _) in enumerate(warehouses.SIZES, 1))

REPORT_STEPS: list[tuple[int, str, str]] = [
    (
        10,
        "Verify the workload was identical (for a full sweep: 18 executions, 1 text, 1 hash, 6 sizes)",
        """
        SELECT COUNT(*)                       AS executions,
               COUNT(DISTINCT query_text)     AS distinct_texts,
               COUNT(DISTINCT query_hash)     AS distinct_hashes,
               COUNT(DISTINCT warehouse_size) AS distinct_sizes,
               ANY_VALUE(query_hash)          AS the_hash
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
        WHERE warehouse_name = '{wh}' AND query_tag LIKE '{tag}%'
          AND query_text ILIKE 'SELECT l_orderkey%'
          AND start_time > DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
        """,
    ),
    (
        11,
        "Which size each run actually ran on",
        """
        SELECT query_tag,
               warehouse_size AS sf_recorded_size,
               query_hash,
               ROUND(total_elapsed_time / 1000, 1) AS elapsed_s,
               execution_status
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
        WHERE warehouse_name = '{wh}' AND query_tag LIKE '{tag}%'
          AND query_text ILIKE 'SELECT l_orderkey%'
          AND start_time > DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
        ORDER BY start_time
        """,
    ),
    (
        12,
        "The sizing curve (median runtime of the finished runs + credits per query)",
        """
        WITH runs AS (
            SELECT warehouse_size AS sz, total_elapsed_time / 1000.0 AS s, execution_status = 'SUCCESS' AS ok
            FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
            WHERE warehouse_name = '{wh}' AND query_tag LIKE '{tag}%'
              AND query_text ILIKE 'SELECT l_orderkey%'
              AND start_time > DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
        ), rate AS (
            {rates}
        )
        SELECT rate.sz AS warehouse_size, rate.cph * {multiplier} AS credits_per_hr, COUNT(*) AS runs,
               COUNT_IF(NOT r.ok)                                                     AS cancelled,
               ROUND(MEDIAN(IFF(r.ok, r.s, NULL)), 1)                                  AS median_s,
               ROUND(rate.cph * {multiplier} * MEDIAN(IFF(r.ok, r.s, NULL)) / 3600, 5) AS est_credits_per_query
        FROM runs r JOIN rate ON rate.sz = r.sz
        GROUP BY rate.sz, rate.cph, rate.ord
        ORDER BY rate.ord
        """,
    ),
    (
        13,
        "Disk spill per size (the reason behind the curve)",
        """
        SELECT warehouse_size,
               COUNT(*)                                                     AS runs,
               ROUND(MAX(bytes_spilled_to_local_storage)  / POW(1024,3), 1) AS gb_spill_local,
               ROUND(MAX(bytes_spilled_to_remote_storage) / POW(1024,3), 1) AS gb_spill_remote,
               MAX(partitions_scanned)                                      AS partitions_scanned,
               MAX(partitions_total)                                        AS partitions_total
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
        WHERE warehouse_name = '{wh}' AND query_tag LIKE '{tag}%'
          AND query_text ILIKE 'SELECT l_orderkey%'
          AND start_time > DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
        GROUP BY warehouse_size
        ORDER BY CASE warehouse_size {size_order} END
        """,
    ),
    (
        14,
        "Billed credits with the 60-second minimum (cancelled runs included: they were billed)",
        """
        WITH runs AS (
            SELECT warehouse_size AS sz, total_elapsed_time / 1000.0 AS s, execution_status = 'SUCCESS' AS ok
            FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
            WHERE warehouse_name = '{wh}' AND query_tag LIKE '{tag}%'
              AND query_text ILIKE 'SELECT l_orderkey%'
              AND start_time > DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
        ), agg AS (
            SELECT sz, SUM(s) AS active_s, COUNT(*) AS n, COUNT_IF(NOT ok) AS cancelled FROM runs GROUP BY sz
        ), rate AS (
            {rates}
        )
        SELECT rate.sz AS warehouse_size, rate.cph * {multiplier} AS credits_per_hr, agg.n AS runs,
               agg.cancelled,
               ROUND(agg.active_s, 1)                                                AS active_s_sum,
               ROUND(GREATEST(agg.active_s, 60) * rate.cph * {multiplier} / 3600, 4) AS billed_cr_with_60s_floor,
               ROUND(agg.active_s * rate.cph * {multiplier} / 3600, 4)               AS billed_cr_no_floor
        FROM agg JOIN rate ON rate.sz = agg.sz
        ORDER BY rate.ord
        """,
    ),
    (
        15,
        "What Snowflake billed the warehouse: every run and setup in the window (can take up to 3 hours to show up)",
        """
        SELECT SUM(credits_used)         AS total_billed_credits,
               SUM(credits_used_compute) AS compute_credits,
               MIN(start_time) AS first_hour, MAX(end_time) AS last_hour
        FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
        WHERE warehouse_name = '{wh}'
          -- Metering rows are hourly, so start from the top of the window's first hour.
          AND start_time >= DATE_TRUNC('hour', DATEADD('hour', -{hours}, CURRENT_TIMESTAMP()))
        HAVING COUNT(*) > 0
        """,
    ),
    (
        16,
        "Billed credits per query (can take up to 8 hours to show up, so run it last)",
        """
        SELECT q.warehouse_size,
               COUNT(*)                                       AS queries,
               ROUND(SUM(a.credits_attributed_compute), 5)    AS billed_credits_total,
               ROUND(AVG(a.credits_attributed_compute), 5)    AS billed_credits_per_query
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY a
        JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q USING (query_id)
        WHERE q.warehouse_name = '{wh}' AND q.query_tag LIKE '{tag}%'
          AND q.query_text ILIKE 'SELECT l_orderkey%'
          AND q.start_time > DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
          AND a.start_time > DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
        GROUP BY q.warehouse_size
        ORDER BY CASE q.warehouse_size {size_order} END
        """,
    ),
]


def report_sql(sql: str, *, warehouse: str, run_id: str, hours: int, generation: str) -> str:
    """Fill a report step's placeholders. Everything formatted in is validated or a constant."""
    if int(hours) < 1:
        raise ValueError(f"hours must be at least 1, got {hours}")
    return sql.format(
        wh=warehouse,
        tag=f"{QUERY_TAG_PREFIX}:{validate_run_id(run_id)}:",
        hours=int(hours),
        multiplier=warehouses.multiplier(generation),
        rates=_RATES,
        size_order=_SIZE_ORDER,
    )
