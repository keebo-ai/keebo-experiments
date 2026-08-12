"""Read multi-cluster history from Snowflake ``ACCOUNT_USAGE``.

Domain layer: each function takes an already-open connection (the injection
seam) and returns normalized records. No ``click`` here — misuse raises plain
``ValueError`` for the CLI to turn into a clean message.

The event names and state below were verified against a live Snowflake account
(August 2026): cluster lifecycle rows use ``RESUME_CLUSTER`` (a cluster starts)
and ``SUSPEND_CLUSTER`` (a cluster stops), are emitted in the ``STARTED`` state,
and carry a ``CLUSTER_NUMBER``. ``SPINDOWN_CLUSTER`` rows exist but have a NULL
``CLUSTER_NUMBER``, so they are filtered out. The alternate names
(``SPINUP_CLUSTER`` / ``SHUTDOWN_CLUSTER``) are kept for accounts that emit them.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Any

_CLUSTER_START_EVENTS = ("RESUME_CLUSTER", "SPINUP_CLUSTER")
_CLUSTER_STOP_EVENTS = ("SUSPEND_CLUSTER", "SPINDOWN_CLUSTER", "SHUTDOWN_CLUSTER")


def _quote_literal(value: str) -> str:
    """Quote a value as a SQL string literal (compared against a column)."""
    return "'" + value.replace("'", "''") + "'"


def _quote_identifier(value: str) -> str:
    """Quote a value as a SQL identifier (case-sensitive; e.g. USE WAREHOUSE).

    Warehouse names can legitimately contain hyphens or be lower-case (created as
    quoted identifiers), so we always double-quote rather than restrict the
    character set.
    """
    return '"' + value.replace('"', '""') + '"'


class ClusterEventKind(enum.Enum):
    STARTED = "started"
    STOPPED = "stopped"


@dataclass(frozen=True)
class ClusterEvent:
    """A single cluster starting or stopping on a multi-cluster warehouse."""

    warehouse: str
    cluster_number: int
    kind: ClusterEventKind
    at: datetime


@dataclass(frozen=True)
class QueryRecord:
    """One executed query and which cluster ran it."""

    warehouse: str
    cluster_number: int
    started_at: datetime
    ended_at: datetime


def _warehouse_filter(warehouse_names: tuple[str, ...]) -> str:
    """Build an optional ``AND WAREHOUSE_NAME IN (...)`` clause."""
    if not warehouse_names:
        return ""
    quoted = ", ".join(_quote_literal(name) for name in warehouse_names)
    return f"  AND WAREHOUSE_NAME IN ({quoted})\n"


def use_warehouse(conn: Any, warehouse: str) -> None:
    """Select the warehouse the read-only history queries run on."""
    cur = conn.cursor()
    try:
        cur.execute(f"USE WAREHOUSE {_quote_identifier(warehouse)}")
    finally:
        cur.close()


def cluster_events(conn: Any, *, days: int, warehouse_names: tuple[str, ...] = ()) -> list[ClusterEvent]:
    """Return completed cluster start/stop events over the last ``days`` days."""
    days = int(days)
    event_list = ", ".join(f"'{name}'" for name in _CLUSTER_START_EVENTS + _CLUSTER_STOP_EVENTS)
    sql = (
        "SELECT TIMESTAMP, WAREHOUSE_NAME, CLUSTER_NUMBER, EVENT_NAME\n"
        "FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_EVENTS_HISTORY\n"
        "WHERE EVENT_STATE = 'STARTED'\n"
        f"  AND EVENT_NAME IN ({event_list})\n"
        "  AND CLUSTER_NUMBER IS NOT NULL\n"
        f"  AND TIMESTAMP >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())\n"
        f"{_warehouse_filter(warehouse_names)}"
        "ORDER BY WAREHOUSE_NAME, CLUSTER_NUMBER, TIMESTAMP"
    )
    cur = conn.cursor()
    try:
        cur.execute(sql)
        rows = cur.fetchall()
    finally:
        cur.close()

    events: list[ClusterEvent] = []
    for timestamp, warehouse_name, cluster_number, event_name in rows:
        kind = ClusterEventKind.STARTED if event_name in _CLUSTER_START_EVENTS else ClusterEventKind.STOPPED
        events.append(ClusterEvent(str(warehouse_name), int(cluster_number), kind, timestamp))
    return events


def query_history(conn: Any, *, days: int, warehouse_names: tuple[str, ...] = ()) -> list[QueryRecord]:
    """Return per-query cluster assignments over the last ``days`` days."""
    days = int(days)
    sql = (
        "SELECT WAREHOUSE_NAME, CLUSTER_NUMBER, START_TIME, END_TIME\n"
        "FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY\n"
        f"WHERE START_TIME >= DATEADD('day', -{days}, CURRENT_TIMESTAMP())\n"
        "  AND CLUSTER_NUMBER IS NOT NULL\n"
        f"{_warehouse_filter(warehouse_names)}"
        "ORDER BY WAREHOUSE_NAME, START_TIME"
    )
    cur = conn.cursor()
    try:
        cur.execute(sql)
        rows = cur.fetchall()
    finally:
        cur.close()

    return [
        QueryRecord(str(warehouse_name), int(cluster_number), start, end)
        for warehouse_name, cluster_number, start, end in rows
    ]
