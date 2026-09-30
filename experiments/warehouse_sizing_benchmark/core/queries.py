"""The SQL and constants behind the warehouse-sizing benchmark.

Kept apart from the orchestration logic so the queries — the part you'd tweak to
change the workload or the reporting — read as data, in one place. No database
or CLI dependencies here.
"""

from __future__ import annotations

from collections.abc import Sequence

from common import warehouses
from common.sql import validate_identifier

__all__ = [
    "BENCHMARK_QUERY",
    "DEFAULT_DATABASE",
    "DEFAULT_MAX_CREDITS",
    "DEFAULT_TABLE",
    "DEFAULT_WAREHOUSE",
    "ELAPSED_SQL",
    "GENERATED_TABLE",
    "GENERATED_TABLE_SQL",
    "LATEST_RUN_SQL",
    "OWNER_COMMENT",
    "QUERY_TAG_PREFIX",
    "REPORT_STEPS",
    "SIZES",
    "SIZE_KEYWORDS",
    "SOURCE_ROWS",
    "SPILL_SQL",
    "query_timeouts",
    "validate_identifier",
]

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

# The sweep covers every standard size, straight from the shared table: each entry
# is (ALTER WAREHOUSE keyword, name recorded in QUERY_HISTORY, credits/hr).
SIZES = warehouses.SIZES
SIZE_KEYWORDS = warehouses.SIZE_KEYWORDS

DEFAULT_TABLE = "SNOWFLAKE_SAMPLE_DATA.TPCH_SF100.LINEITEM"
DEFAULT_WAREHOUSE = "SIZING_BENCHMARK_WH"
DEFAULT_DATABASE = "SIZING_BENCHMARK_DB"
QUERY_TAG_PREFIX = "wsbench"

# Marks the warehouse and database as the benchmark's own. Nothing is resized,
# suspended, or dropped without it. It's the comment earlier versions put on
# the warehouse, so a warehouse they created is still recognised.
OWNER_COMMENT = "Keebo warehouse-sizing benchmark - safe to drop"

# --------------------------------------------------------------------------- #
# The generated fallback table
#
# Used only when the role can't read the SNOWFLAKE_SAMPLE_DATA share. `setup`
# then generates a table with LINEITEM's columns and TPCH_SF100's row count in
# the benchmark's own database. Every value is a hash of the row number, and
# order keys repeat four times, as in TPC-H, so the GROUP BY has the same shape.
# --------------------------------------------------------------------------- #
GENERATED_TABLE = "PUBLIC.LINEITEM"  # inside the benchmark database
SOURCE_ROWS = 600_000_000
SETUP_TIMEOUT_SECONDS = 1800  # generating the table: at most 0.5 credits on a Gen1 X-Small

GENERATED_TABLE_SQL = """
CREATE TABLE {table} AS
SELECT FLOOR(seq / 4) + 1                                            AS l_orderkey,
       ABS(HASH(seq, 1)) % 20000000 + 1                              AS l_partkey,
       ABS(HASH(seq, 2)) % 1000000 + 1                               AS l_suppkey,
       ABS(HASH(seq, 3)) % 50 + 1                                    AS l_quantity,
       (ABS(HASH(seq, 4)) % 10400000 + 90000) / 100                  AS l_extendedprice,
       (ABS(HASH(seq, 5)) % 11) / 100                                AS l_discount,
       DATEADD('day', ABS(HASH(seq, 6)) % 2500, '1992-01-01'::DATE)  AS l_shipdate
FROM (SELECT SEQ8() AS seq FROM TABLE(GENERATOR(ROWCOUNT => {rows})))
"""

# --------------------------------------------------------------------------- #
# The cost cap
#
# ``--max-credits`` is a ceiling on what a run bills, Snowflake's 60-second
# minimums included. Each size resumes once, and bills at least 60 seconds for
# it, so that much is reserved first. The rest is split evenly across the run's
# queries (sizes x runs) and enforced as each size's STATEMENT_TIMEOUT_IN_SECONDS:
# a query on a size billing C credits/hr may run share * 3600 / C seconds.
# Snowflake cancels anything longer. The full article sweep (6 sizes x 3 runs)
# bills about 1.3 credits, comfortably inside the default.
# --------------------------------------------------------------------------- #
DEFAULT_MAX_CREDITS = 3.0
MIN_TIMEOUT_SECONDS = 10


def query_timeouts(keywords: Sequence[str], *, generation: str, runs: int, max_credits: float) -> dict[str, int]:
    """Each size's per-query timeout so the whole run stays within ``max_credits``."""
    rates = {keyword: warehouses.credits_per_hour(keyword, generation) for keyword in keywords}
    minimums = sum(rate * warehouses.BILLING_MINIMUM_SECONDS / 3600 for rate in rates.values())
    budget = max_credits - minimums
    if budget <= 0:
        raise ValueError(
            f"--max-credits {max_credits:g} doesn't cover Snowflake's 60-second minimum for these sizes "
            f"({minimums:.2f} credits). Raise --max-credits or pick fewer --size."
        )
    share = budget / (len(rates) * runs)
    timeouts = {keyword: int(share * 3600 / rate) for keyword, rate in rates.items()}
    tightest = min(timeouts, key=timeouts.get)
    if timeouts[tightest] < MIN_TIMEOUT_SECONDS:
        raise ValueError(
            f"--max-credits {max_credits:g} gives each {warehouses.SIZE_LABEL[tightest]} query only "
            f"{timeouts[tightest]}s. Raise --max-credits, or pick fewer --size / --runs."
        )
    return timeouts


# --------------------------------------------------------------------------- #
# Live per-query stats
#
# ACCOUNT_USAGE lags minutes, so `run` reads two faster sources right after each
# query, to show results as they happen:
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
# Every benchmark query is tagged ``wsbench:<run id>:<size>:<attempt>``, and the
# report reads one run: the latest by default. ``{tag}`` is that run's tag
# prefix, ``{wh}`` the upper-cased warehouse name, ``{hours}`` the lookback.
# All are validated before being formatted in.
# --------------------------------------------------------------------------- #
LATEST_RUN_SQL = """
SELECT MAX(SPLIT_PART(query_tag, ':', 2)) AS run_id
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE warehouse_name = '{wh}'
  AND query_tag LIKE 'wsbench:%'
  AND start_time > DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
"""

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
               ROUND(total_elapsed_time / 1000, 1) AS elapsed_s
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
        WHERE warehouse_name = '{wh}' AND query_tag LIKE '{tag}%'
          AND query_text ILIKE 'SELECT l_orderkey%'
          AND start_time > DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
        ORDER BY start_time
        """,
    ),
    (
        12,
        "The sizing curve (median runtime + estimated credits per query, Gen1 rates)",
        """
        WITH runs AS (
            SELECT warehouse_size AS sz, total_elapsed_time / 1000.0 AS s
            FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
            WHERE warehouse_name = '{wh}' AND query_tag LIKE '{tag}%'
              AND query_text ILIKE 'SELECT l_orderkey%'
              AND start_time > DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
        ), rate AS (
            SELECT 'X-Small' sz, 1 cph, 1 ord
            UNION ALL SELECT 'Small',2,2
            UNION ALL SELECT 'Medium',4,3
            UNION ALL SELECT 'Large',8,4
            UNION ALL SELECT 'X-Large',16,5
            UNION ALL SELECT '2X-Large',32,6
        )
        SELECT rate.sz AS warehouse_size, rate.cph AS credits_per_hr, COUNT(*) AS runs,
               ROUND(MEDIAN(r.s), 1)                   AS median_s,
               ROUND(rate.cph * MEDIAN(r.s) / 3600, 5) AS est_credits_per_query
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
        ORDER BY CASE warehouse_size
                 WHEN 'X-Small' THEN 1 WHEN 'Small' THEN 2 WHEN 'Medium' THEN 3
                 WHEN 'Large' THEN 4 WHEN 'X-Large' THEN 5 ELSE 6 END
        """,
    ),
    (
        14,
        "Billed credits with the 60-second minimum (Gen1 rates)",
        """
        WITH runs AS (
            SELECT warehouse_size AS sz, total_elapsed_time / 1000.0 AS s
            FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
            WHERE warehouse_name = '{wh}' AND query_tag LIKE '{tag}%'
              AND query_text ILIKE 'SELECT l_orderkey%'
              AND start_time > DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
        ), agg AS (
            SELECT sz, SUM(s) AS active_s, COUNT(*) AS n FROM runs GROUP BY sz
        ), rate AS (
            SELECT 'X-Small' sz, 1 cph, 1 ord
            UNION ALL SELECT 'Small',2,2
            UNION ALL SELECT 'Medium',4,3
            UNION ALL SELECT 'Large',8,4
            UNION ALL SELECT 'X-Large',16,5
            UNION ALL SELECT '2X-Large',32,6
        )
        SELECT rate.sz AS warehouse_size, rate.cph AS credits_per_hr, agg.n AS runs,
               ROUND(agg.active_s, 1)                                  AS active_s_sum,
               ROUND(GREATEST(agg.active_s, 60) * rate.cph / 3600, 4)  AS billed_cr_with_60s_floor,
               ROUND(agg.active_s * rate.cph / 3600, 4)                AS billed_cr_no_floor
        FROM agg JOIN rate ON rate.sz = agg.sz
        ORDER BY rate.ord
        """,
    ),
    (
        15,
        "The authoritative billed total for the warehouse: every run and setup in the window (lags up to 3h)",
        """
        SELECT SUM(credits_used)         AS total_billed_credits,
               SUM(credits_used_compute) AS compute_credits,
               MIN(start_time) AS first_hour, MAX(end_time) AS last_hour
        FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
        WHERE warehouse_name = '{wh}'
          AND start_time > DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
        HAVING COUNT(*) > 0
        """,
    ),
    (
        16,
        "Billed credits per query (highest latency view — run last)",
        """
        SELECT q.warehouse_size,
               COUNT(*)                                       AS queries,
               ROUND(SUM(a.credits_attributed_compute), 5)    AS billed_credits_total,
               ROUND(AVG(a.credits_attributed_compute), 5)    AS billed_credits_per_query
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY a
        JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q USING (query_id)
        WHERE q.warehouse_name = '{wh}' AND q.query_tag LIKE '{tag}%'
          AND q.query_text ILIKE 'SELECT l_orderkey%'
          AND a.start_time > DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
        GROUP BY q.warehouse_size
        ORDER BY CASE q.warehouse_size
                 WHEN 'X-Small' THEN 1 WHEN 'Small' THEN 2 WHEN 'Medium' THEN 3
                 WHEN 'Large' THEN 4 WHEN 'X-Large' THEN 5 ELSE 6 END
        """,
    ),
]
