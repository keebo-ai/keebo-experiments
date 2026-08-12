"""Warehouse-sizing benchmark — domain layer (no CLI dependencies).

Run one fixed query across every Snowflake warehouse size, then read the timings
and credits back from Snowflake's own history. This is the logic behind the
Keebo article "Run the warehouse-sizing benchmark yourself".

Every function here takes an already-open connection as its first argument — the
injection seam — so the same code runs against a connection opened by the CLI, a
notebook, or a test fake. The module never imports ``click``; it raises plain
``ValueError`` for misuse and the CLI layer turns that into a clean error
message.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------------------- #
# The benchmark workload
#
# This SELECT is identical on every run at every size, so any difference in
# timing is the warehouse, not the query. ``IDENTIFIER($lineitem_table)`` reads
# the table from a session variable set in `sweep_sizes` (Step 2 of the article).
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

# A no-op progress sink; the CLI passes ``click.echo`` instead.
Echo = Callable[[str], None]


def _silent(_message: str) -> None:
    pass


# Identifiers we interpolate into SQL are validated against this first —
# belt-and-suspenders against injection, since they arrive from CLI flags.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.$]+$")


def validate_identifier(value: str, label: str) -> str:
    """Return ``value`` if it is a safe SQL identifier, else raise ``ValueError``."""
    if not _IDENTIFIER_RE.match(value):
        raise ValueError(f"{label} must match [A-Za-z0-9_.$]+, got {value!r}")
    return value


@dataclass(frozen=True)
class ReportTable:
    """One reporting query's result: its step number, title, and rows."""

    step: int
    title: str
    columns: list[str]
    rows: list[tuple[Any, ...]]


def sweep_sizes(
    conn: Any,
    *,
    table: str = DEFAULT_TABLE,
    warehouse_name: str = DEFAULT_WAREHOUSE,
    sizes: Sequence[tuple[str, str, int]] = tuple(SIZES),
    runs: int = 3,
    echo: Echo = _silent,
) -> None:
    """Create the warehouse and run the fixed query across each size (Steps 1-9).

    The measurable output of the sweep lives in Snowflake's query history, not in
    a return value; :func:`read_report` reads it back. ``echo`` receives
    human-readable progress as the sweep runs.
    """
    validate_identifier(table, "table")
    validate_identifier(warehouse_name, "warehouse")

    cur = conn.cursor()
    try:
        # Step 1: confirm the sample data is mounted.
        cur.execute("SHOW TERSE OBJECTS LIKE 'LINEITEM' IN SCHEMA SNOWFLAKE_SAMPLE_DATA.TPCH_SF100")
        if not cur.fetchall() and table == DEFAULT_TABLE:
            raise ValueError(
                "SNOWFLAKE_SAMPLE_DATA.TPCH_SF100.LINEITEM not found. An "
                "ACCOUNTADMIN can mount it:\n"
                "  CREATE DATABASE IF NOT EXISTS SNOWFLAKE_SAMPLE_DATA "
                "FROM SHARE SFC_SAMPLES.SAMPLE_DATA;\n"
                "  GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE_SAMPLE_DATA "
                "TO ROLE PUBLIC;"
            )

        # Step 2: point a session variable at the table.
        cur.execute(f"SET lineitem_table = '{table}'")

        # Step 3: create and select the dedicated benchmark warehouse.
        echo(f"Creating warehouse {warehouse_name} ...")
        cur.execute(
            f"CREATE WAREHOUSE IF NOT EXISTS {warehouse_name} "
            "WAREHOUSE_SIZE = XSMALL AUTO_SUSPEND = 60 AUTO_RESUME = TRUE "
            "INITIALLY_SUSPENDED = TRUE "
            "COMMENT = 'Keebo warehouse-sizing benchmark - safe to drop'"
        )
        cur.execute(f"USE WAREHOUSE {warehouse_name}")
        # Turn off the result cache, else a repeated query returns for free and
        # defeats the measurement.
        cur.execute("ALTER SESSION SET USE_CACHED_RESULT = FALSE")

        # Steps 4-9: the sweep.
        for keyword, label, _credits in sizes:
            echo(f"\n=== {label} ({keyword}) ===")
            cur.execute(f"ALTER WAREHOUSE {warehouse_name} SET WAREHOUSE_SIZE = {keyword}")
            cur.execute(f"ALTER WAREHOUSE {warehouse_name} RESUME IF SUSPENDED")
            for attempt in range(1, runs + 1):
                cur.execute(f"ALTER SESSION SET QUERY_TAG = '{QUERY_TAG_PREFIX}:{keyword}:{attempt}'")
                started = time.perf_counter()
                cur.execute(BENCHMARK_QUERY)
                cur.fetchall()  # force full execution
                elapsed = time.perf_counter() - started
                warmth = "cold" if attempt == 1 else "warm"
                echo(f"  run {attempt} ({warmth}): {elapsed:6.1f}s  [{cur.sfqid}]")
            # SUSPEND clears the local cache so the next size also starts cold.
            cur.execute(f"ALTER WAREHOUSE {warehouse_name} SUSPEND")

        echo(
            "\nSweep complete. ACCOUNT_USAGE lags a few minutes (up to ~45), so "
            "wait, then run:  warehouse-sizing-benchmark report"
        )
    finally:
        cur.close()


def read_report(
    conn: Any,
    *,
    warehouse_name: str = DEFAULT_WAREHOUSE,
    hours: int = 6,
) -> list[ReportTable]:
    """Read timings and credits back from ACCOUNT_USAGE (Steps 10-16).

    Returns one :class:`ReportTable` per step. Empty ``rows`` mean ACCOUNT_USAGE
    hasn't caught up yet — wait and rerun.
    """
    validate_identifier(warehouse_name, "warehouse")
    hours = int(hours)

    cur = conn.cursor()
    tables: list[ReportTable] = []
    try:
        for step, title, sql in REPORT_STEPS:
            cur.execute(sql.format(hours=hours, wh=warehouse_name))
            columns = [col[0] for col in cur.description]
            tables.append(ReportTable(step, title, columns, list(cur.fetchall())))
    finally:
        cur.close()
    return tables


def drop_warehouse(
    conn: Any,
    *,
    warehouse_name: str = DEFAULT_WAREHOUSE,
    echo: Echo = _silent,
) -> None:
    """Drop the benchmark warehouse and nothing else (Step 17)."""
    validate_identifier(warehouse_name, "warehouse")
    cur = conn.cursor()
    try:
        cur.execute(f"DROP WAREHOUSE IF EXISTS {warehouse_name}")
        echo(f"Dropped {warehouse_name}.")
    finally:
        cur.close()


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
