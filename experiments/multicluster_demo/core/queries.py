"""SQL for the multi-cluster demo: warehouse DDL, the workload, and read-back.

Domain layer: functions take an open connection and issue SQL. No ``click``.
Read-back uses ``INFORMATION_SCHEMA.QUERY_HISTORY`` (near real-time) rather than
``ACCOUNT_USAGE`` (which lags up to ~45 min) so the demo can report immediately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

DEFAULT_WAREHOUSE = "KEEBO_MULTICLUSTER_DEMO_WH"
DEFAULT_SIZE = "XSMALL"
DEFAULT_CONCURRENCY = 16
DEFAULT_MAX_CLUSTERS = 3
# SF10 keeps each query to a few seconds on XSMALL — long enough to queue and
# benefit from scale-out, short enough that the whole demo stays cheap and fast.
DEFAULT_TABLE = "SNOWFLAKE_SAMPLE_DATA.TPCH_SF10.LINEITEM"

# A deliberately low per-cluster concurrency ceiling so the batch reliably
# queues on a single cluster (and so multi-cluster visibly relieves it) without
# needing a huge, expensive workload. This is the mechanism being demonstrated:
# more concurrent queries than a cluster's slots -> the extras queue.
DEFAULT_CONCURRENCY_LEVEL = 4

# Safety cap so a single query can never hang the run (or leak a warehouse).
DEFAULT_STATEMENT_TIMEOUT_SECONDS = 120

# A fixed, compute-bound aggregate: TPC-H Q1 plus an exact COUNT(DISTINCT) over
# the high-cardinality order key. The distinct count keeps each query running for
# several seconds even when the data is cached, so a single cluster stays
# saturated long enough for multi-cluster scale-out to actually shorten the
# batch (a purely cached scan finishes before a new cluster can help).
_WORKLOAD_TEMPLATE = (
    "SELECT L_RETURNFLAG, L_LINESTATUS, COUNT(*) AS rows_,\n"
    "       SUM(L_QUANTITY) AS sum_qty, AVG(L_DISCOUNT) AS avg_disc,\n"
    "       SUM(L_EXTENDEDPRICE * (1 - L_DISCOUNT)) AS revenue,\n"
    "       COUNT(DISTINCT L_ORDERKEY) AS distinct_orders\n"
    "FROM {table}\n"
    "GROUP BY L_RETURNFLAG, L_LINESTATUS\n"
    "ORDER BY L_RETURNFLAG, L_LINESTATUS"
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
# A dotted, fully-qualified table name: db.schema.table (each part an identifier).
_QUALIFIED_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*){0,2}$")


def _quote_identifier(value: str) -> str:
    """Quote a value as a case-sensitive SQL identifier (e.g. a warehouse name)."""
    return '"' + value.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    """Quote a value as a SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


def _validate_table(table: str) -> str:
    if not _QUALIFIED_NAME.match(table):
        raise ValueError(f"Unsafe table name: {table!r}")
    return table


def workload_query(table: str = DEFAULT_TABLE) -> str:
    """The fixed workload query against ``table`` (validated as a qualified name)."""
    return _WORKLOAD_TEMPLATE.format(table=_validate_table(table))


def database_of(table: str = DEFAULT_TABLE) -> str:
    """The database component of a fully-qualified table name."""
    return _validate_table(table).split(".")[0]


def create_warehouse(
    conn: Any,
    *,
    name: str,
    size: str,
    concurrency_level: int = DEFAULT_CONCURRENCY_LEVEL,
    statement_timeout_seconds: int = DEFAULT_STATEMENT_TIMEOUT_SECONDS,
) -> None:
    """Create the dedicated demo warehouse (single cluster, suspended)."""
    if not _IDENTIFIER.match(size):
        raise ValueError(f"Unsafe warehouse size: {size!r}")
    cur = conn.cursor()
    try:
        cur.execute(
            f"CREATE WAREHOUSE IF NOT EXISTS {_quote_identifier(name)}\n"
            f"  WAREHOUSE_SIZE = '{size.upper()}'\n"
            "  MIN_CLUSTER_COUNT = 1\n"
            "  MAX_CLUSTER_COUNT = 1\n"
            "  SCALING_POLICY = 'STANDARD'\n"
            f"  MAX_CONCURRENCY_LEVEL = {int(concurrency_level)}\n"
            f"  STATEMENT_TIMEOUT_IN_SECONDS = {int(statement_timeout_seconds)}\n"
            "  AUTO_SUSPEND = 60\n"
            "  AUTO_RESUME = TRUE\n"
            "  INITIALLY_SUSPENDED = TRUE"
        )
    finally:
        cur.close()


def drop_warehouse(conn: Any, name: str) -> None:
    """Drop the demo warehouse."""
    cur = conn.cursor()
    try:
        cur.execute(f"DROP WAREHOUSE IF EXISTS {_quote_identifier(name)}")
    finally:
        cur.close()


def set_cluster_bounds(conn: Any, *, name: str, min_clusters: int, max_clusters: int) -> None:
    """Set the warehouse's cluster range for the next round."""
    cur = conn.cursor()
    try:
        cur.execute(
            f"ALTER WAREHOUSE {_quote_identifier(name)} "
            f"SET MIN_CLUSTER_COUNT = {int(min_clusters)} MAX_CLUSTER_COUNT = {int(max_clusters)}"
        )
    finally:
        cur.close()


def session_setup(*, warehouse: str, query_tag: str) -> list[str]:
    """The per-session setup statements each concurrent worker runs first.

    ``USE_CACHED_RESULT = FALSE`` is essential: without it, the second round
    would be served from Snowflake's result cache (the queries are identical),
    finish instantly, and never actually run on the warehouse — so nothing would
    queue and no cluster would spin up. Disabling it forces real execution every
    time, which is the whole point of the demo.
    """
    return [
        f"USE WAREHOUSE {_quote_identifier(warehouse)}",
        f"ALTER SESSION SET QUERY_TAG = {_quote_literal(query_tag)}",
        "ALTER SESSION SET USE_CACHED_RESULT = FALSE",
    ]


@dataclass(frozen=True)
class QueryStat:
    """One tagged workload query, read back from INFORMATION_SCHEMA."""

    cluster_number: int | None
    queued_overload_ms: int
    elapsed_ms: int
    started_at: datetime
    ended_at: datetime
    status: str


def read_round_stats(conn: Any, *, database: str, warehouse: str, query_tag: str) -> list[QueryStat]:
    """Read back the tagged workload queries for one round (near real-time).

    Qualified by ``database`` so it works even when the connection has no default
    database set; the QUERY_HISTORY table function returns account-wide history
    regardless of which database's INFORMATION_SCHEMA hosts it.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT CLUSTER_NUMBER, QUEUED_OVERLOAD_TIME, TOTAL_ELAPSED_TIME,\n"
            "       START_TIME, END_TIME, EXECUTION_STATUS\n"
            f"FROM TABLE({_quote_identifier(database)}.INFORMATION_SCHEMA.QUERY_HISTORY(RESULT_LIMIT => 1000))\n"
            f"WHERE QUERY_TAG = {_quote_literal(query_tag)}\n"
            f"  AND WAREHOUSE_NAME = {_quote_literal(warehouse)}\n"
            "  AND QUERY_TYPE = 'SELECT'\n"
            "ORDER BY START_TIME"
        )
        rows = cur.fetchall()
    finally:
        cur.close()

    return [
        QueryStat(
            cluster_number=None if cluster_number is None else int(cluster_number),
            queued_overload_ms=int(queued_ms or 0),
            elapsed_ms=int(elapsed_ms or 0),
            started_at=started_at,
            ended_at=ended_at,
            status=str(status),
        )
        for cluster_number, queued_ms, elapsed_ms, started_at, ended_at, status in rows
    ]
