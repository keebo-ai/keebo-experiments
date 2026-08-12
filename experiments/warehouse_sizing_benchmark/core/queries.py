"""The SQL and constants behind the warehouse-sizing benchmark.

Kept apart from the orchestration logic so the queries — the part you'd tweak to
change the workload or the reporting — read as data, in one place. No database
or CLI dependencies here.
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------- #
# The benchmark workload
#
# This SELECT is identical on every run at every size, so any difference in
# timing is the warehouse, not the query. ``IDENTIFIER($lineitem_table)`` reads
# the table from a session variable set by the sweep (Step 2 of the article).
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

# Each entry: (ALTER WAREHOUSE keyword, name recorded in QUERY_HISTORY, credits/hr).
SIZES: list[tuple[str, str, int]] = [
    ("XSMALL", "X-Small", 1),
    ("SMALL", "Small", 2),
    ("MEDIUM", "Medium", 4),
    ("LARGE", "Large", 8),
    ("XLARGE", "X-Large", 16),
    ("XXLARGE", "2X-Large", 32),
]
SIZE_KEYWORDS = [keyword for keyword, _, _ in SIZES]

DEFAULT_TABLE = "SNOWFLAKE_SAMPLE_DATA.TPCH_SF100.LINEITEM"
DEFAULT_WAREHOUSE = "SIZING_BENCHMARK_WH"
QUERY_TAG_PREFIX = "wsbench"

# Identifiers we interpolate into SQL are validated against this first —
# belt-and-suspenders against injection, since they arrive from CLI flags.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.$]+$")


def validate_identifier(value: str, label: str) -> str:
    """Return ``value`` if it is a safe SQL identifier, else raise ``ValueError``."""
    if not _IDENTIFIER_RE.match(value):
        raise ValueError(f"{label} must match [A-Za-z0-9_.$]+, got {value!r}")
    return value


# --------------------------------------------------------------------------- #
# Reporting queries (Steps 10-16)
#
# ``{hours}`` bounds the lookback window; ``{wh}`` names the metering row in
# Step 15. Both are validated (int / identifier) before being formatted in.
# --------------------------------------------------------------------------- #
REPORT_STEPS: list[tuple[int, str, str]] = [
    (
        10,
        "Verify the workload was identical (expect 18 executions, 1 text, 1 hash, 6 sizes)",
        """
        SELECT COUNT(*)                       AS executions,
               COUNT(DISTINCT query_text)     AS distinct_texts,
               COUNT(DISTINCT query_hash)     AS distinct_hashes,
               COUNT(DISTINCT warehouse_size) AS distinct_sizes,
               ANY_VALUE(query_hash)          AS the_hash
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
        WHERE query_tag LIKE 'wsbench:%'
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
        WHERE query_tag LIKE 'wsbench:%'
          AND query_text ILIKE 'SELECT l_orderkey%'
          AND start_time > DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
        ORDER BY start_time
        """,
    ),
    (
        12,
        "The sizing curve (median runtime + estimated credits per query)",
        """
        WITH runs AS (
            SELECT warehouse_size AS sz, total_elapsed_time / 1000.0 AS s
            FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
            WHERE query_tag LIKE 'wsbench:%' AND query_text ILIKE 'SELECT l_orderkey%'
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
        WHERE query_tag LIKE 'wsbench:%' AND query_text ILIKE 'SELECT l_orderkey%'
          AND start_time > DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
        GROUP BY warehouse_size
        ORDER BY CASE warehouse_size
                 WHEN 'X-Small' THEN 1 WHEN 'Small' THEN 2 WHEN 'Medium' THEN 3
                 WHEN 'Large' THEN 4 WHEN 'X-Large' THEN 5 ELSE 6 END
        """,
    ),
    (
        14,
        "Billed credits with the 60-second minimum",
        """
        WITH runs AS (
            SELECT warehouse_size AS sz, total_elapsed_time / 1000.0 AS s
            FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
            WHERE query_tag LIKE 'wsbench:%' AND query_text ILIKE 'SELECT l_orderkey%'
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
        "The authoritative billed total (should land near 1.3 credits)",
        """
        SELECT SUM(credits_used)         AS total_billed_credits,
               SUM(credits_used_compute) AS compute_credits,
               MIN(start_time) AS first_hour, MAX(end_time) AS last_hour
        FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
        WHERE warehouse_name = '{wh}'
          AND start_time > DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
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
        WHERE q.query_tag LIKE 'wsbench:%'
          AND a.start_time > DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
        GROUP BY q.warehouse_size
        ORDER BY CASE q.warehouse_size
                 WHEN 'X-Small' THEN 1 WHEN 'Small' THEN 2 WHEN 'Medium' THEN 3
                 WHEN 'Large' THEN 4 WHEN 'X-Large' THEN 5 ELSE 6 END
        """,
    ),
]
