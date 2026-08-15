"""The run engine: drive the warehouses and record what happened.

Takes an already-open connection as its first argument — the injection seam —
so it runs against a connection opened by the CLI, a notebook, or a test fake.
No ``click`` here; misuse raises plain ``ValueError`` and the CLI turns that
into a clean error message.

Time is injected too. ``clock`` supplies the timestamps that end up in the
manifest and ``sleep`` does the waiting, so the tests can drive a 75-second idle
without spending 75 seconds.
"""

from __future__ import annotations

import textwrap
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from common.sql import validate_identifier
from experiments.multi_cluster_billing.core import manifest, queries

Echo = Callable[[str], None]
Clock = Callable[[], datetime]
Sleep = Callable[[float], None]

#: Width the run's own commentary wraps to. Terminal output during the run, not
#: the report file, which the CLI lays out for itself.
ECHO_WIDTH = 92


def _silent(_message: str) -> None:
    pass


def describe(spec: queries.ScenarioSpec, *, why: bool) -> list[str]:
    """The scenario's account of itself, wrapped into lines for ``echo``.

    ``does`` on every replicate, so a reader watching the run always knows what
    the warehouse in front of them is doing. ``why`` only the first time a
    scenario appears, because the reason does not change between replicates and
    repeating it four times buries the run's actual progress.
    """
    lines = _wrapped("  what it does:   ", spec.does)
    if why:
        lines += _wrapped("  why it matters: ", spec.why)
    return lines


def _wrapped(label: str, text: str) -> list[str]:
    """``label`` then ``text``, wrapped to the echo width and hanging-indented."""
    wrapped = textwrap.wrap(
        text,
        width=ECHO_WIDTH,
        initial_indent=label,
        subsequent_indent=" " * len(label),
    )
    return wrapped or [label.rstrip()]


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class WarehouseState:
    """The `SHOW WAREHOUSES` fields this experiment cares about.

    The last two are settings rather than state: they are read back once per
    warehouse so the run aborts when the warehouse it created is not the
    warehouse it asked for. ``None`` means the account did not report the
    column, which is not the same as reporting a wrong value.
    """

    state: str
    started_clusters: int
    queued: int
    enable_query_acceleration: bool | None = None
    resource_constraint: str | None = None


def _as_bool(value: object) -> bool | None:
    """Decode a SHOW WAREHOUSES flag, which is sometimes text and sometimes not."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "yes", "y", "1"}


def show_warehouse(cur, warehouse: str) -> WarehouseState:
    """Read the warehouse's current state.

    Columns are mapped by name from ``cur.description``: `SHOW WAREHOUSES`
    returns a wide result whose column order is not a stable contract.
    """
    validate_identifier(warehouse, "warehouse")
    cur.execute(queries.SHOW_WAREHOUSE.format(wh=warehouse))
    rows = cur.fetchall()
    if not rows:
        raise ValueError(f"warehouse {warehouse} not found")

    index = {column[0].lower(): position for position, column in enumerate(cur.description)}
    row = rows[0]

    def field(name: str, default: object = None) -> object:
        position = index.get(name)
        return default if position is None or row[position] is None else row[position]

    # The generation has two column names, matching the two DDL properties. An
    # account that takes GENERATION tends to report it back under that name.
    constraint = field("resource_constraint")
    if constraint is None:
        generation = field("generation")
        constraint = None if generation is None else queries.resource_constraint_for_generation(generation)
    return WarehouseState(
        state=str(field("state", "UNKNOWN")),
        started_clusters=int(field("started_clusters", 0)),
        queued=int(field("queued", 0)),
        enable_query_acceleration=_as_bool(field("enable_query_acceleration")),
        resource_constraint=None if constraint is None else str(constraint).upper(),
    )


def assert_settings(state: WarehouseState, *, warehouse: str, resource_constraint: str) -> None:
    """Abort the run when the warehouse is not the one the design assumes.

    Both checks guard a measurement, not a preference. Query acceleration
    changes how long a query occupies a cluster; the generation changes the
    credit rate the bill is decoded at. A setting the account does not report is
    accepted rather than guessed.
    """
    if state.enable_query_acceleration:
        raise ValueError(
            f"warehouse {warehouse} reports ENABLE_QUERY_ACCELERATION = TRUE. "
            "It changes how long a query occupies a cluster, and so when Snowflake decides to scale out. "
            "Aborting rather than measuring a different experiment."
        )
    expected = resource_constraint.upper()
    if state.resource_constraint is not None and state.resource_constraint != expected:
        raise ValueError(
            f"warehouse {warehouse} reports generation {state.resource_constraint}, expected {expected}. "
            "The credit rate depends on the generation; a mismatch here is what made v1 inconclusive."
        )


def wait_for(
    cur,
    warehouse: str,
    predicate: Callable[[WarehouseState], bool],
    *,
    timeout: float = queries.POLL_TIMEOUT_SECONDS,
    interval: float = queries.POLL_INTERVAL_SECONDS,
    clock: Clock = _now,
    sleep: Sleep | None = None,
    polls: list[manifest.Poll] | None = None,
) -> WarehouseState | None:
    """Poll until ``predicate`` holds, recording every observation.

    Returns the matching state, or ``None`` if ``timeout`` elapsed first. A
    timeout is not an exception: the caller still has to suspend the warehouse,
    and a rep that never reached its target cluster count is a result the
    verdict knows how to reject.
    """
    sleeper: Sleep = sleep if sleep is not None else time.sleep
    started = clock()
    while True:
        state = show_warehouse(cur, warehouse)
        if polls is not None:
            polls.append(
                manifest.Poll(
                    at=clock().isoformat(),
                    state=state.state,
                    started_clusters=state.started_clusters,
                    queued=state.queued,
                )
            )
        if predicate(state):
            return state
        if (clock() - started).total_seconds() >= timeout:
            return None
        sleeper(interval)


def sleep_until(
    anchor: datetime,
    offset: float,
    *,
    clock: Clock,
    sleeper: Sleep,
    echo: Echo,
    label: str,
) -> None:
    """Sleep until ``offset`` seconds after ``anchor``, never past it.

    The cycle's landmarks are fixed offsets from the resume, so a slow resume or
    a slow provisioning wait eats into the sleep rather than pushing the
    landmark later. Every replicate then has the same shape, and its warehouse
    seconds are comparable with every other replicate's.
    """
    remaining = offset - (clock() - anchor).total_seconds()
    if remaining < 0:
        echo(f"  WARNING {label} is {abs(remaining):.1f}s late; the cycle ran long")
    if remaining <= 0:
        return
    sleeper(remaining)


def run_cycle(
    cur,
    *,
    warehouse: str,
    spec: queries.ScenarioSpec,
    replicate: int,
    size: str,
    resource_constraint: str,
    poll_timeout: float = queries.POLL_TIMEOUT_SECONDS,
    poll_interval: float = queries.POLL_INTERVAL_SECONDS,
    clock: Clock = _now,
    sleep: Sleep | None = None,
    echo: Echo = _silent,
) -> manifest.Replicate:
    """One warehouse, driven through one resume / scale-out / suspend cycle.

    Three properties of the ordering carry the design:

    1. Both landmarks are fixed offsets from the **resume**, so every scenario's
       warehouse runs for the same wall-clock time whatever provisioning did.
       Anchoring the suspend on the scale-out instead would make a slow
       scale-out lengthen the warehouse, and the bill with it.
    2. The scale-out lands where its scenario asks for it, which is the whole
       point of having more than one scenario. Most of them start the extra
       cluster after the warehouse is past its own first minute, so what the
       extra cluster costs cannot be confused with what the resume cost. The
       `inside` scenario deliberately does the opposite and starts its cluster
       within that first minute, because whether a cluster started there is
       covered by the minute already paid for is one of the questions.
    3. ``SUSPEND`` is issued **before** ``MIN_CLUSTER_COUNT`` is reset to 1.
       Resetting first would make the extra clusters drainable while the
       warehouse is still running, handing their lifetimes to the undocumented
       scale-in gate this design exists to avoid.

    Every scenario issues the same statements in the same order, including the
    single-cluster ones — where ``SET MIN_CLUSTER_COUNT = 1`` is a no-op. A
    scenario that differs only in a number cannot differ in its side effects.

    A cluster that never appears is recorded, not raised: the warehouse still
    has to be suspended, and the report knows how to flag the replicate.
    """
    validate_identifier(warehouse, "warehouse")
    sleeper: Sleep = sleep if sleep is not None else time.sleep
    polls: list[manifest.Poll] = []
    label = f"  {spec.name} r{replicate}"

    echo(f"{label}: resuming {warehouse}")
    resumed_at = clock()
    cur.execute(queries.RESUME.format(wh=warehouse))
    wait_for(
        cur,
        warehouse,
        lambda s: s.state.upper() == "STARTED" and s.started_clusters >= 1,
        timeout=poll_timeout,
        interval=poll_interval,
        clock=clock,
        sleep=sleeper,
        polls=polls,
    )
    resume_confirmed_at = clock()

    sleep_until(resumed_at, spec.scale_out_at_seconds, clock=clock, sleeper=sleeper, echo=echo, label="the scale-out")

    scaled_at = clock()
    cur.execute(queries.SET_MIN_CLUSTERS.format(wh=warehouse, n=spec.target_clusters))

    reached: WarehouseState | None = None
    if spec.target_clusters > 1:
        echo(f"{label}: waiting for {spec.target_clusters} clusters")
        # Bounded by what is left of the cycle as well as by the poll timeout:
        # the suspend happens at its fixed offset whether or not the clusters
        # arrived, because a replicate that ran long is not comparable with one
        # that did not.
        left = max(0.0, spec.cycle_seconds - (clock() - resumed_at).total_seconds())
        reached = wait_for(
            cur,
            warehouse,
            lambda s: s.started_clusters >= spec.target_clusters,
            timeout=min(poll_timeout, left),
            interval=poll_interval,
            clock=clock,
            sleep=sleeper,
            polls=polls,
        )
        if reached is None:
            echo(f"{label}: WARNING cluster count never reached {spec.target_clusters}")
    target_seen_at = clock() if (reached is not None or spec.target_clusters == 1) else None

    sleep_until(resumed_at, spec.cycle_seconds, clock=clock, sleeper=sleeper, echo=echo, label="the suspend")

    # SUSPEND first — see the docstring.
    suspend_issued_at = clock()
    cur.execute(queries.SUSPEND.format(wh=warehouse))
    cur.execute(queries.SET_MIN_CLUSTERS.format(wh=warehouse, n=1))
    wait_for(
        cur,
        warehouse,
        lambda s: s.state.upper() in {"SUSPENDED", "SUSPENDING"} or s.started_clusters == 0,
        timeout=poll_timeout,
        interval=poll_interval,
        clock=clock,
        sleep=sleeper,
        polls=polls,
    )
    suspend_confirmed_at = clock()

    return manifest.Replicate(
        scenario=spec.name,
        index=replicate,
        warehouse=warehouse,
        size=size.upper(),
        resource_constraint=resource_constraint.upper(),
        target_clusters=spec.target_clusters,
        cycle_seconds=spec.cycle_seconds,
        kind=spec.kind,
        resumed_at=resumed_at.isoformat(),
        resume_confirmed_at=resume_confirmed_at.isoformat(),
        scaled_at=scaled_at.isoformat(),
        target_seen_at=None if target_seen_at is None else target_seen_at.isoformat(),
        suspend_issued_at=suspend_issued_at.isoformat(),
        suspend_confirmed_at=suspend_confirmed_at.isoformat(),
        max_started_clusters=max((p.started_clusters for p in polls), default=0),
        query_ids=[],
        polls=polls,
        error=None,
    )


def _await_queries(
    conn,
    query_ids: list[str],
    *,
    interval: float,
    timeout: float,
    clock: Clock,
    sleeper: Sleep,
) -> None:
    """Block until every async query finishes, or ``timeout`` elapses."""
    started = clock()
    pending = list(query_ids)
    while pending:
        pending = [qid for qid in pending if conn.is_still_running(conn.get_query_status(qid))]
        if not pending:
            return
        if (clock() - started).total_seconds() >= timeout:
            return
        sleeper(interval)


def run_natural_rep(
    conn,
    cur,
    *,
    warehouse: str,
    spec: queries.ScenarioSpec = queries.NATURAL,
    replicate: int = 1,
    size: str = queries.DEFAULT_SIZE,
    resource_constraint: str = queries.DEFAULT_RESOURCE_CONSTRAINT,
    rowcount: int = queries.DEFAULT_NATURAL_ROWCOUNT,
    idle_seconds: int = queries.CONTROL.scale_out_at_seconds,
    poll_timeout: float = queries.POLL_TIMEOUT_SECONDS,
    poll_interval: float = queries.POLL_INTERVAL_SECONDS,
    previous_warehouse: str | None = None,
    clock: Clock = _now,
    sleep: Sleep | None = None,
    echo: Echo = _silent,
) -> manifest.Replicate:
    """Let Snowflake start the second cluster itself, and record the same shape.

    The warehouse is created with ``MAX_CONCURRENCY_LEVEL = 1``, so of the two
    queries submitted together the first occupies cluster 1 and the second
    queues — which is what makes the STANDARD policy scale out. Both are
    submitted with ``execute_async`` on separate cursors of the same connection,
    so they run concurrently server-side without a second connection, and the
    polling loop keeps working while they are in flight (``SHOW WAREHOUSES``
    needs no warehouse).

    ``scaled_at`` is ``None`` in the returned rep: nothing forced the scale-out,
    which is the whole point of the scenario.
    """
    validate_identifier(warehouse, "warehouse")
    sleeper: Sleep = sleep if sleep is not None else time.sleep
    polls: list[manifest.Poll] = []

    echo(f"  natural: resuming {warehouse}")
    resumed_at = clock()
    cur.execute(queries.RESUME.format(wh=warehouse))
    wait_for(
        cur,
        warehouse,
        lambda s: s.state.upper() == "STARTED" and s.started_clusters >= 1,
        timeout=poll_timeout,
        interval=poll_interval,
        clock=clock,
        sleep=sleeper,
        polls=polls,
    )
    resume_confirmed_at = clock()

    echo(f"  natural: idling {idle_seconds}s")
    sleeper(idle_seconds)

    cur.execute(queries.USE.format(wh=warehouse))
    sql = queries.natural_query(rowcount)
    query_ids: list[str] = []
    for _ in range(2):
        query_cursor = conn.cursor()
        query_cursor.execute_async(sql)
        query_ids.append(query_cursor.sfqid)
    echo(f"  natural: submitted 2 concurrent queries ({', '.join(query_ids)})")

    reached = wait_for(
        cur,
        warehouse,
        lambda s: s.started_clusters >= 2,
        timeout=poll_timeout,
        interval=poll_interval,
        clock=clock,
        sleep=sleeper,
        polls=polls,
    )
    target_seen_at = clock() if reached is not None else None
    if reached is None:
        echo("  natural: WARNING Snowflake never started a second cluster")

    _await_queries(
        conn,
        query_ids,
        interval=poll_interval,
        timeout=poll_timeout,
        clock=clock,
        sleeper=sleeper,
    )

    suspend_issued_at = clock()
    cur.execute(queries.SUSPEND.format(wh=warehouse))
    wait_for(
        cur,
        warehouse,
        lambda s: s.state.upper() in {"SUSPENDED", "SUSPENDING"} or s.started_clusters == 0,
        timeout=poll_timeout,
        interval=poll_interval,
        clock=clock,
        sleep=sleeper,
        polls=polls,
    )
    suspend_confirmed_at = clock()

    if previous_warehouse:
        validate_identifier(previous_warehouse, "warehouse")
        cur.execute(queries.USE.format(wh=previous_warehouse))

    return manifest.Replicate(
        scenario=spec.name,
        index=replicate,
        warehouse=warehouse,
        size=size.upper(),
        resource_constraint=resource_constraint.upper(),
        target_clusters=spec.target_clusters,
        # 0 because nothing fixed this cycle's length: it ran as long as the two
        # queries took, which is what makes it a cross-check rather than a
        # scenario the verdict weighs.
        cycle_seconds=0,
        kind=spec.kind,
        resumed_at=resumed_at.isoformat(),
        resume_confirmed_at=resume_confirmed_at.isoformat(),
        scaled_at=None,
        target_seen_at=None if target_seen_at is None else target_seen_at.isoformat(),
        suspend_issued_at=suspend_issued_at.isoformat(),
        suspend_confirmed_at=suspend_confirmed_at.isoformat(),
        max_started_clusters=max((p.started_clusters for p in polls), default=0),
        query_ids=query_ids,
        polls=polls,
        error=None,
    )


def session_info(cur) -> tuple[str, str, str, str | None]:
    """Account, region, Snowflake version, and the session's current warehouse.

    Recorded in the manifest so a result years from now still says where and
    when it was measured.
    """
    cur.execute(queries.SESSION_INFO)
    rows = cur.fetchall()
    if not rows:
        return ("", "", "", None)
    account, region, version, warehouse = (list(rows[0]) + [None] * 4)[:4]
    return (str(account or ""), str(region or ""), str(version or ""), warehouse)


def check_account_usage(cur) -> str | None:
    """Return ``None`` if ACCOUNT_USAGE is readable, else the error text.

    A warning rather than a failure, but issued now: the user should learn their
    role cannot read the meter at run time, not three hours later when the
    report is finally due.
    """
    try:
        cur.execute(queries.ACCOUNT_USAGE_PROBE)
        cur.fetchall()
    except Exception as exc:  # noqa: BLE001 - the connector raises many error types
        return str(exc)
    return None


def schedule(specs: list[queries.ScenarioSpec], replicates: int) -> list[tuple[queries.ScenarioSpec, int]]:
    """Order the run as a Latin square: (spec, replicate index) in run order.

    The warehouses run sequentially over about an hour. Anything drifting over
    that window — account load, region behaviour — would otherwise correlate with
    scenario, because the obvious loop runs all four `control` replicates
    together. In block ``r``, position ``p`` runs ``specs[(r + p) % len(specs)]``,
    so each scenario occupies each position exactly once.
    """
    count = int(replicates)
    if count < 1:
        raise ValueError(f"replicates must be 1 or greater, got {replicates!r}")
    if not specs:
        raise ValueError("at least one scenario is needed")

    plan: list[tuple[queries.ScenarioSpec, int]] = []
    taken: dict[str, int] = {}
    for block in range(count):
        for position in range(len(specs)):
            spec = specs[(block + position) % len(specs)]
            taken[spec.name] = taken.get(spec.name, 0) + 1
            plan.append((spec, taken[spec.name]))
    return plan


def check_resource_monitors(cur) -> list[str]:
    """Name any resource monitor on the account, or return an empty list.

    A monitor that suspends a warehouse mid-cycle would silently corrupt a
    replicate — the warehouse would stop early and the bill would look like a
    shorter cycle. Worth a warning up front; never fatal, and never fatal if the
    role cannot read them either.
    """
    try:
        cur.execute(queries.SHOW_RESOURCE_MONITORS)
        rows = cur.fetchall()
    except Exception:  # noqa: BLE001 - the connector raises many error types
        return []
    index = {column[0].lower(): position for position, column in enumerate(cur.description)}
    position = index.get("name", 0)
    return [str(row[position]) for row in rows if row and row[position] is not None]


#: Words that mark a CREATE WAREHOUSE failure as an entitlement problem rather
#: than a syntax one. Only then is the edition worth raising: appending it to a
#: syntax error points the reader away from the actual cause.
_EDITION_MARKERS = ("multi-cluster", "multi cluster", "edition", "not enabled", "not supported", "unsupported")


def _edition_hint(exc: Exception) -> str:
    text = str(exc).lower()
    if not any(marker in text for marker in _EDITION_MARKERS):
        return ""
    return "\nMulti-cluster warehouses require the Enterprise edition or higher."


def run_experiment(
    conn,
    *,
    size: str = queries.DEFAULT_SIZE,
    prefix: str = queries.WAREHOUSE_PREFIX,
    resource_constraint: str = queries.DEFAULT_RESOURCE_CONSTRAINT,
    replicates: int = queries.DEFAULT_REPLICATES,
    include_natural: bool = True,
    natural_rowcount: int = queries.DEFAULT_NATURAL_ROWCOUNT,
    poll_timeout: float = queries.POLL_TIMEOUT_SECONDS,
    poll_interval: float = queries.POLL_INTERVAL_SECONDS,
    clock: Clock = _now,
    sleep: Sleep | None = None,
    echo: Echo = _silent,
    checkpoint: Callable[[manifest.RunManifest], None] | None = None,
) -> manifest.RunManifest:
    """Run the Latin square and return the manifest describing what happened.

    Warehouses run one at a time. Running them concurrently would halve the wall
    clock but puts them in contention for provisioning on the same account, which
    is the one variable this design most wants to hold still.

    ``checkpoint`` is called with the manifest after every warehouse. The run
    creates 29 warehouses over about an hour; writing the file only at the end
    would orphan every one of them on a crash, leaving `cleanup` nothing to work
    from.

    Each scenario introduces itself the first time it runs — what it makes the
    warehouse do, and which billing question its numbers can settle. A run that
    takes an hour and spends real credits should say what it is buying while it
    is buying it, not only afterwards in the report.
    """
    # Raises on a bad size or generation before anything is created.
    queries.published_credits_per_hour(size, resource_constraint)

    started_at = clock()
    token = manifest.run_token(started_at)

    cur = conn.cursor()
    try:
        account, region, version, previous_warehouse = session_info(cur)

        usage_error = check_account_usage(cur)
        if usage_error:
            echo(f"WARNING: this role cannot read ACCOUNT_USAGE, so `report` will fail: {usage_error}")

        monitors = check_resource_monitors(cur)
        if monitors:
            echo(
                f"WARNING: resource monitor(s) {', '.join(monitors)} exist on this account. One that suspends a "
                "warehouse mid-cycle would silently shorten a replicate."
            )

        record = manifest.RunManifest(
            schema_version=manifest.SCHEMA_VERSION,
            run_token=token,
            account=account,
            region=region,
            snowflake_version=version,
            size=size.upper(),
            resource_constraint=resource_constraint.upper(),
            started_at=started_at.isoformat(),
            ended_at=None,
            replicates=[],
        )

        plan = schedule(queries.MEASURED_SCENARIOS, replicates)
        if include_natural:
            plan.append((queries.NATURAL, 1))

        introduced: set[str] = set()
        for spec, replicate in plan:
            name = queries.warehouse_name(prefix, spec.name, replicate, token)
            echo(f"\n=== {spec.name} r{replicate} ({name}) ===")
            for line in describe(spec, why=spec.name not in introduced):
                echo(line)
            introduced.add(spec.name)
            try:
                cur.execute(
                    queries.create_warehouse_sql(name, size=size, spec=spec, resource_constraint=resource_constraint)
                )
            except ValueError:
                raise
            except Exception as exc:  # noqa: BLE001 - the connector raises many error types
                raise ValueError(
                    f"could not create the multi-cluster warehouse {name}: {exc}{_edition_hint(exc)}"
                ) from exc

            assert_settings(show_warehouse(cur, name), warehouse=name, resource_constraint=resource_constraint)

            if spec.kind == "natural":
                item = run_natural_rep(
                    conn,
                    cur,
                    warehouse=name,
                    spec=spec,
                    replicate=replicate,
                    size=size,
                    resource_constraint=resource_constraint,
                    rowcount=natural_rowcount,
                    poll_timeout=poll_timeout,
                    poll_interval=poll_interval,
                    previous_warehouse=previous_warehouse,
                    clock=clock,
                    sleep=sleep,
                    echo=echo,
                )
            else:
                item = run_cycle(
                    cur,
                    warehouse=name,
                    spec=spec,
                    replicate=replicate,
                    size=size,
                    resource_constraint=resource_constraint,
                    poll_timeout=poll_timeout,
                    poll_interval=poll_interval,
                    clock=clock,
                    sleep=sleep,
                    echo=echo,
                )

            record.replicates.append(item)
            if checkpoint is not None:
                checkpoint(record)

        record.ended_at = clock().isoformat()
        if checkpoint is not None:
            checkpoint(record)
        return record
    finally:
        cur.close()


def drop_warehouses(conn, *, warehouses: list[str], echo: Echo = _silent) -> None:
    """Drop exactly the warehouses named, and nothing else."""
    cur = conn.cursor()
    try:
        for name in warehouses:
            validate_identifier(name, "warehouse")
            cur.execute(queries.DROP.format(wh=name))
            echo(f"Dropped {name}.")
    finally:
        cur.close()
