"""Run and tear down the benchmark warehouse (Steps 1-9 and 17).

Both functions take an already-open connection as their first argument — the
injection seam — so they run against a connection opened by the CLI, a notebook,
or a test fake. No ``click`` here; misuse raises plain ``ValueError`` and the CLI
turns that into a clean error message.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any

from experiments.warehouse_sizing_benchmark.core import queries

# A no-op progress sink; the CLI passes ``click.echo`` instead.
Echo = Callable[[str], None]


def _silent(_message: str) -> None:
    pass


def sweep_sizes(
    conn: Any,
    *,
    table: str = queries.DEFAULT_TABLE,
    warehouse_name: str = queries.DEFAULT_WAREHOUSE,
    sizes: Sequence[tuple[str, str, int]] = tuple(queries.SIZES),
    runs: int = 3,
    echo: Echo = _silent,
) -> None:
    """Create the warehouse and run the fixed query across each size (Steps 1-9).

    The measurable output of the sweep lives in Snowflake's query history, not in
    a return value; ``report.read_report`` reads it back. ``echo`` receives
    human-readable progress as the sweep runs.
    """
    queries.validate_identifier(table, "table")
    queries.validate_identifier(warehouse_name, "warehouse")

    cur = conn.cursor()
    try:
        # Step 1: confirm the sample data is mounted.
        cur.execute("SHOW TERSE OBJECTS LIKE 'LINEITEM' IN SCHEMA SNOWFLAKE_SAMPLE_DATA.TPCH_SF100")
        if not cur.fetchall() and table == queries.DEFAULT_TABLE:
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
                cur.execute(f"ALTER SESSION SET QUERY_TAG = '{queries.QUERY_TAG_PREFIX}:{keyword}:{attempt}'")
                started = time.perf_counter()
                cur.execute(queries.BENCHMARK_QUERY)
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


def drop_warehouse(
    conn: Any,
    *,
    warehouse_name: str = queries.DEFAULT_WAREHOUSE,
    echo: Echo = _silent,
) -> None:
    """Drop the benchmark warehouse and nothing else (Step 17)."""
    queries.validate_identifier(warehouse_name, "warehouse")
    cur = conn.cursor()
    try:
        cur.execute(f"DROP WAREHOUSE IF EXISTS {warehouse_name}")
        echo(f"Dropped {warehouse_name}.")
    finally:
        cur.close()
