"""Warehouse and per-cluster lifetimes derived from WAREHOUSE_EVENTS_HISTORY.

ACCOUNT_USAGE returns rows sharing a timestamp in no fixed order, and a
transition's request row (``RESUME_WAREHOUSE``, ``ALTER_WAREHOUSE``) carries the
same timestamp as the ``WAREHOUSE_CONSISTENT`` row that completes it. Everything
here first imposes a total order, then reads lifetimes off that order
positionally, so the completion marker for a resize is never mistaken for the
one that closes the suspend.

Pure: no I/O, no Snowflake connection, no click.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from experiments.multi_cluster_billing.core.queries import EVENT_PHASE_RANK, UNKNOWN_PHASE_RANK

RESUME_WAREHOUSE = "RESUME_WAREHOUSE"
SUSPEND_WAREHOUSE = "SUSPEND_WAREHOUSE"
RESUME_CLUSTER = "RESUME_CLUSTER"
SPINUP_CLUSTER = "SPINUP_CLUSTER"
SUSPEND_CLUSTER = "SUSPEND_CLUSTER"
WAREHOUSE_CONSISTENT = "WAREHOUSE_CONSISTENT"

#: Rows that can start a cluster's life, best marker first.
CLUSTER_START_EVENTS = (RESUME_CLUSTER, SPINUP_CLUSTER)


@dataclass(frozen=True)
class Event:
    """One row of WAREHOUSE_EVENTS_HISTORY."""

    warehouse: str
    cluster_number: int | None
    name: str
    reason: str | None
    state: str | None
    at: datetime


def phase_rank(name: str) -> int:
    """Where ``name`` sits within a single timestamp.

    The completion marker ranks last so it follows whatever it completes.
    """
    return EVENT_PHASE_RANK.get(name.upper(), UNKNOWN_PHASE_RANK)


def order_events(events: list[Event]) -> list[Event]:
    """Impose the same total order the SQL asks for, whatever order rows arrive in."""
    return sorted(
        events,
        key=lambda e: (e.at, phase_rank(e.name), -1 if e.cluster_number is None else int(e.cluster_number)),
    )


def parse_rows(columns: list[str], rows: list[tuple]) -> list[Event]:
    """Map cursor rows onto :class:`Event` by column name, not position."""
    index = {name.lower(): position for position, name in enumerate(columns)}

    def field_of(row: tuple, name: str):
        position = index.get(name)
        return row[position] if position is not None else None

    parsed: list[Event] = []
    for row in rows:
        cluster = field_of(row, "cluster_number")
        parsed.append(
            Event(
                warehouse=field_of(row, "warehouse_name"),
                cluster_number=None if cluster is None else int(cluster),
                name=field_of(row, "event_name"),
                reason=field_of(row, "event_reason"),
                state=field_of(row, "event_state"),
                at=field_of(row, "timestamp"),
            )
        )
    return parsed


@dataclass(frozen=True)
class Lifetimes:
    """How long one warehouse and each of its clusters were actually running."""

    warehouse: str
    warehouse_seconds: float | None
    cluster_seconds: dict[int, float]
    #: Seconds from the moment the warehouse started to the moment each cluster
    #: started. One of the candidate billing rules charges a cluster only for
    #: the part of it that runs past the warehouse's first minute, so when a
    #: cluster started matters as much as how long it ran.
    cluster_start_offsets: dict[int, float] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    @property
    def cluster_seconds_total(self) -> float:
        """What Snowflake bills against: the sum over every cluster."""
        return sum(self.cluster_seconds.values())

    @property
    def extra_cluster_seconds(self) -> list[float]:
        """Lifetimes of the clusters beyond the first, in cluster order."""
        return [seconds for number, seconds in sorted(self.cluster_seconds.items()) if number != 1]

    @property
    def extra_clusters(self) -> list[tuple[float, float]]:
        """``(start_offset, seconds)`` for each cluster beyond the first.

        Everything the billing rules need to price an extra cluster, in cluster
        order. A cluster whose start offset was never established is reported as
        starting at zero; ``complete`` is false in that case, so it is never
        priced.
        """
        return [
            (self.cluster_start_offsets.get(number, 0.0), seconds)
            for number, seconds in sorted(self.cluster_seconds.items())
            if number != 1
        ]

    @property
    def complete(self) -> bool:
        return self.warehouse_seconds is not None and not self.missing


def _seconds(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds()


def _first(events: list[Event], name: str, *, cluster: int | None = None) -> int | None:
    """Index of the first ``name`` row, optionally for one cluster."""
    for position, event in enumerate(events):
        if event.name == name and (cluster is None or event.cluster_number == cluster):
            return position
    return None


def _next_marker(events: list[Event], after: int) -> datetime | None:
    """Timestamp of the first completion marker at or after position ``after``.

    Positional, so a mid-cycle resize consumes its own marker and the suspend
    still finds the one that belongs to it.
    """
    for event in events[after:]:
        if event.name == WAREHOUSE_CONSISTENT:
            return event.at
    return None


def _cluster_start(events: list[Event], cluster: int) -> int | None:
    for name in CLUSTER_START_EVENTS:
        position = _first(events, name, cluster=cluster)
        if position is not None:
            return position
    return None


def derive(events: list[Event], warehouse: str, *, expected_clusters: int) -> Lifetimes:
    """Read one warehouse's lifetimes off its event log.

    Anything the log does not show is reported in ``missing`` rather than
    guessed: a partial event set must never be billed against.
    """
    ordered = order_events([e for e in events if e.warehouse == warehouse])
    missing: list[str] = []

    warehouse_seconds: float | None = None
    warehouse_started: datetime | None = None
    resume_at = _first(ordered, RESUME_WAREHOUSE)
    suspend_at = _first(ordered, SUSPEND_WAREHOUSE)
    if resume_at is None:
        missing.append(f"no {RESUME_WAREHOUSE}")
    if suspend_at is None:
        missing.append(f"no {SUSPEND_WAREHOUSE}")
    if resume_at is not None and suspend_at is not None:
        started = _next_marker(ordered, resume_at)
        ended = _next_marker(ordered, suspend_at)
        if started is None or ended is None:
            missing.append(f"no {WAREHOUSE_CONSISTENT} closing the warehouse interval")
        else:
            warehouse_started = started
            warehouse_seconds = _seconds(started, ended)

    numbers = sorted({e.cluster_number for e in ordered if e.cluster_number is not None})
    cluster_seconds: dict[int, float] = {}
    cluster_start_offsets: dict[int, float] = {}
    for number in numbers:
        start = _cluster_start(ordered, number)
        stop = _first(ordered, SUSPEND_CLUSTER, cluster=number)
        if start is None:
            missing.append(f"no {RESUME_CLUSTER} for cluster {number}")
            continue
        if stop is None:
            missing.append(f"no {SUSPEND_CLUSTER} for cluster {number}")
            continue
        cluster_seconds[number] = _seconds(ordered[start].at, ordered[stop].at)
        if warehouse_started is not None:
            # Cluster 1 starts within a fraction of a second of the warehouse,
            # either side of it, so this can be very slightly negative. It is
            # left as measured rather than clamped; nothing downstream reads
            # cluster 1's offset, and the extras are seconds later.
            cluster_start_offsets[number] = _seconds(warehouse_started, ordered[start].at)

    if len(cluster_seconds) < expected_clusters:
        missing.append(f"expected {expected_clusters} clusters, recovered {len(cluster_seconds)}")

    return Lifetimes(
        warehouse=warehouse,
        warehouse_seconds=warehouse_seconds,
        cluster_seconds=cluster_seconds,
        cluster_start_offsets=cluster_start_offsets,
        missing=missing,
    )
