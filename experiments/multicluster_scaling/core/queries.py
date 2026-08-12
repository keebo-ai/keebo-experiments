"""Read multi-cluster history from Snowflake ``ACCOUNT_USAGE``.

Domain layer: each function takes an already-open connection (the injection
seam) and returns normalized records. No ``click`` here — misuse raises plain
``ValueError`` for the CLI to turn into a clean message.

The ``EVENT_NAME`` values were verified against Snowflake's
``WAREHOUSE_EVENTS_HISTORY`` documentation (August 2026); they are centralized
here so they are easy to adjust if an account emits different values.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

_CLUSTER_START_EVENTS = ("SPINUP_CLUSTER", "RESUME_CLUSTER")
_CLUSTER_STOP_EVENTS = ("SPINDOWN_CLUSTER", "SHUTDOWN_CLUSTER")

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def validate_identifier(value: str, kind: str) -> str:
    """Guard a value we interpolate into SQL as an identifier/name."""
    if not _IDENTIFIER.match(value):
        raise ValueError(
            f"Unsafe {kind} name {value!r}: expected letters, digits, underscore, "
            "or '$', with a non-digit first character."
        )
    return value


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
    """Build an optional ``AND WAREHOUSE_NAME IN (...)`` clause (validated)."""
    if not warehouse_names:
        return ""
    quoted = ", ".join(f"'{validate_identifier(name, 'warehouse')}'" for name in warehouse_names)
    return f"  AND WAREHOUSE_NAME IN ({quoted})\n"


def use_warehouse(conn: Any, warehouse: str) -> None:
    """Select the warehouse the read-only history queries run on."""
    validate_identifier(warehouse, "warehouse")
    cur = conn.cursor()
    try:
        cur.execute(f"USE WAREHOUSE {warehouse}")
    finally:
        cur.close()


def cluster_events(conn: Any, *, days: int, warehouse_names: tuple[str, ...] = ()) -> list[ClusterEvent]:
    """Return completed cluster start/stop events over the last ``days`` days."""
    days = int(days)
    event_list = ", ".join(f"'{name}'" for name in _CLUSTER_START_EVENTS + _CLUSTER_STOP_EVENTS)
    sql = (
        "SELECT TIMESTAMP, WAREHOUSE_NAME, CLUSTER_NUMBER, EVENT_NAME\n"
        "FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_EVENTS_HISTORY\n"
        "WHERE EVENT_STATE = 'COMPLETED'\n"
        f"  AND EVENT_NAME IN ({event_list})\n"
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
