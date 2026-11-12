"""Create, check, and drop the experiment's own Snowflake objects.

``setup`` creates the experiment's **home** objects:

- an X-Small Gen1 **warehouse** that does setup's one-off copy and runs the
  report's ``ACCOUNT_USAGE`` reads (it never runs a measured workload), and
- a transient **database** holding a copy of ``LINEITEM`` sorted by ship date,
  which the DML workload clones,

each marked with :data:`queries.OWNER_COMMENT`. It also checks, for free, that
the account can create Gen2 and Adaptive warehouses. ``run`` creates one more
warehouse per cell and drops it as soon as the cell is done. Its name starts
with the home warehouse's, so ``cleanup`` finds any a crashed run left behind
and drops those along with the home objects. Nothing without the mark is ever
touched. Every function is safe to rerun.

Functions take an open connection (or cursor). No ``click`` here; problems
raise ``ValueError`` and the CLI turns them into clean messages.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from common import dedicated, warehouses
from common.snowflake import is_statement_timeout, table_exists
from common.sql import validate_name
from experiments.adaptive_compute.core import queries

Echo = Callable[[str], None]


def _silent(_message: str) -> None:
    """The default progress sink; the CLI passes ``click.echo`` instead."""


@dataclass(frozen=True)
class HomeObjects:
    """The names of the home warehouse and database. Build it with :meth:`named`, which validates them."""

    warehouse: str
    database: str

    @classmethod
    def named(cls, warehouse: str = queries.DEFAULT_WAREHOUSE, database: str = queries.DEFAULT_DATABASE) -> HomeObjects:
        return cls(validate_name(warehouse, "warehouse"), validate_name(database, "database"))

    @property
    def cell_prefix(self) -> str:
        """What every warehouse the experiment creates besides the home one starts with."""
        return f"{self.warehouse}_"


_NOT_SET_UP = "Run `keebo-experiments adaptive-compute setup` first (with the same --warehouse / --database)."


def setup(conn: Any, objects: HomeObjects, *, scale: int = queries.DEFAULT_SCALE, echo: Echo = _silent) -> None:
    """Create the home warehouse and database, copy LINEITEM, and check what the account supports."""
    schema = queries.sample_schema(scale)
    if scale not in queries.BASE_COPY_SIZE:
        raise ValueError(
            f"setup doesn't copy TPCH_SF{scale}: sorting it would cost several credits. Use --scale "
            f"{max(queries.BASE_COPY_SIZE)} or smaller, or run without the dml workload."
        )
    cur = conn.cursor()
    try:
        cur.execute("SELECT CURRENT_ROLE()")
        echo(f"Using role {cur.fetchall()[0][0]} (it needs CREATE WAREHOUSE and CREATE DATABASE).")
        if not table_exists(cur, f"{schema}.LINEITEM"):
            raise ValueError(
                f"this role can't read {schema}. The experiment needs Snowflake's SNOWFLAKE_SAMPLE_DATA share "
                "(see the README's Requirements)."
            )

        # Check both names before creating anything, so a name that's taken leaves nothing behind.
        warehouse_row = dedicated.claim(cur, "WAREHOUSE", objects.warehouse, comment=queries.OWNER_COMMENT)
        database_row = dedicated.claim(cur, "DATABASE", objects.database, comment=queries.OWNER_COMMENT)
        if warehouse_row is None:
            echo(f"Creating warehouse {objects.warehouse} (X-Small, Gen1) ...")
            cur.execute(
                f"CREATE WAREHOUSE {objects.warehouse} WAREHOUSE_SIZE = XSMALL GENERATION = '1' "
                "AUTO_SUSPEND = 60 AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE "
                f"COMMENT = '{queries.OWNER_COMMENT}'"
            )
        else:
            echo(f"Reusing warehouse {objects.warehouse}.")
        if database_row is None:
            echo(f"Creating database {objects.database} (transient, so no Time Travel or Fail-safe storage) ...")
            cur.execute(
                f"CREATE TRANSIENT DATABASE {objects.database} "
                f"DATA_RETENTION_TIME_IN_DAYS = 0 COMMENT = '{queries.OWNER_COMMENT}'"
            )
        else:
            echo(f"Reusing database {objects.database}.")

        set_idle(cur, objects.warehouse)
        cur.execute(f"USE WAREHOUSE {objects.warehouse}")
        try:
            base = queries.base_table(objects.database, scale)
            if table_exists(cur, base):
                echo(f"Reusing table {base}.")
            else:
                _copy_base_table(cur, objects, scale=scale, echo=echo)
            _check_account_usage(cur, echo)
        finally:
            # Silent: the warehouse may never have resumed if everything already existed.
            dedicated.suspend_quietly(cur, objects.warehouse)

        _probe_warehouse_types(cur, objects, echo)
        echo("\nReady. Next, run:  keebo-experiments adaptive-compute run")
    finally:
        cur.close()


def require(cur: Any, objects: HomeObjects, *, scale: int, need_base: bool) -> None:
    """Check the home objects exist and are ours, and that the data the run reads is there."""
    require_warehouse(cur, objects)
    if dedicated.claim(cur, "DATABASE", objects.database, comment=queries.OWNER_COMMENT) is None:
        raise ValueError(f"database {objects.database} doesn't exist. {_NOT_SET_UP}")
    schema = queries.sample_schema(scale)
    if not table_exists(cur, f"{schema}.LINEITEM"):
        raise ValueError(f"this role can't read {schema}, which the workloads read.")
    if need_base and not table_exists(cur, queries.base_table(objects.database, scale)):
        raise ValueError(
            f"{queries.base_table(objects.database, scale)} doesn't exist, and the dml workload clones it. "
            f"Run `keebo-experiments adaptive-compute setup --scale {scale}`, or leave out the dml workload."
        )


def require_warehouse(cur: Any, objects: HomeObjects) -> None:
    """Check the home warehouse exists and is ours."""
    if dedicated.claim(cur, "WAREHOUSE", objects.warehouse, comment=queries.OWNER_COMMENT) is None:
        raise ValueError(f"warehouse {objects.warehouse} doesn't exist. {_NOT_SET_UP}")


def cleanup(conn: Any, objects: HomeObjects, *, everything: bool = False, echo: Echo = _silent) -> list[str]:
    """Drop what the experiment created, then check nothing's left. Returns whatever still is.

    Always drops the home warehouse and database (the database holds the
    LINEITEM copy and any scratch table), and every cell or probe warehouse
    named after the home warehouse. Objects carrying the experiment's mark
    under *other* names (from a ``setup`` with a different ``--warehouse`` or
    ``--database``) are listed, and dropped too with ``everything``. Nothing
    without the mark is ever touched.
    """
    cur = conn.cursor()
    try:
        for name in dedicated.owned(cur, "WAREHOUSE", objects.cell_prefix, comment=queries.OWNER_COMMENT):
            dedicated.drop(cur, "WAREHOUSE", name, comment=queries.OWNER_COMMENT)
            echo(f"Dropped leftover warehouse {name}.")
        for kind, name in (("WAREHOUSE", objects.warehouse), ("DATABASE", objects.database)):
            dropped = dedicated.drop(cur, kind, name, comment=queries.OWNER_COMMENT)
            echo(f"Dropped {kind.lower()} {name}." if dropped else f"No {kind.lower()} {name} to drop.")

        others = _everything_marked(cur)
        if others and everything:
            for kind, name in others:
                dedicated.drop(cur, kind, name, comment=queries.OWNER_COMMENT)
                echo(f"Dropped {kind.lower()} {name} (from a setup under another name).")
        elif others:
            echo("These carry the experiment's mark but other names, so they were left alone:")
            for kind, name in others:
                echo(f"  {kind.lower()} {name}")
            echo("Rerun with --everything to drop them too.")

        remaining = [f"{kind.lower()} {name}" for kind, name in _everything_marked(cur)]
        echo(
            "Checked: nothing the experiment created is left on the account."
            if not remaining
            else f"Still on the account: {', '.join(remaining)}."
        )
        return remaining
    finally:
        cur.close()


def _everything_marked(cur: Any) -> list[tuple[str, str]]:
    """Every warehouse and database carrying the experiment's mark, whatever it's called."""
    return [
        (kind, name)
        for kind in ("WAREHOUSE", "DATABASE")
        for name in dedicated.owned(cur, kind, comment=queries.OWNER_COMMENT)
    ]


def _copy_base_table(cur: Any, objects: HomeObjects, *, scale: int, echo: Echo) -> None:
    """Copy LINEITEM sorted by ship date, on a bigger size with its own timeout, then go back to X-Small."""
    size = queries.BASE_COPY_SIZE[scale]
    base = queries.base_table(objects.database, scale)
    rate = warehouses.credits_per_hour(size, "1")
    echo(
        f"Copying TPCH_SF{scale}.LINEITEM into {base}, sorted by ship date, on a {warehouses.SIZE_LABEL[size]} "
        f"(at most {queries.BASE_COPY_TIMEOUT_SECONDS // 60} minutes, "
        f"{rate * queries.BASE_COPY_TIMEOUT_SECONDS / 3600:.2f} credits) ..."
    )
    cur.execute(
        f"ALTER WAREHOUSE {objects.warehouse} SET WAREHOUSE_SIZE = {size} "
        f"STATEMENT_TIMEOUT_IN_SECONDS = {queries.BASE_COPY_TIMEOUT_SECONDS}"
    )
    try:
        cur.execute(queries.BASE_COPY_SQL.format(table=base, schema=queries.sample_schema(scale)))
    except Exception as exc:
        if not is_statement_timeout(exc):
            raise
        raise ValueError(
            f"copying LINEITEM hit its {queries.BASE_COPY_TIMEOUT_SECONDS}s cap, so nothing was created. "
            "Try a smaller --scale, or leave out the dml workload."
        ) from exc
    finally:
        reset_to_idle(cur, objects.warehouse)


def _probe_warehouse_types(cur: Any, objects: HomeObjects, echo: Echo) -> None:
    """Check the account can create Gen2 and Adaptive warehouses, by creating and dropping one of each.

    Free: a standard warehouse created suspended bills nothing, and Adaptive
    bills nothing until it runs a query.
    """
    probes = {
        "Gen2": (
            f"{objects.cell_prefix}PROBE_G2",
            "CREATE WAREHOUSE {name} WAREHOUSE_SIZE = XSMALL GENERATION = '2' INITIALLY_SUSPENDED = TRUE "
            "AUTO_SUSPEND = 60 COMMENT = '{comment}'",
        ),
        "Adaptive": (
            f"{objects.cell_prefix}PROBE_AD",
            "CREATE WAREHOUSE {name} WITH WAREHOUSE_TYPE = 'ADAPTIVE' COMMENT = '{comment}'",
        ),
    }
    for label, (name, ddl) in probes.items():
        try:
            if dedicated.claim(cur, "WAREHOUSE", name, comment=queries.OWNER_COMMENT) is None:
                cur.execute(ddl.format(name=name, comment=queries.OWNER_COMMENT))
            dedicated.drop(cur, "WAREHOUSE", name, comment=queries.OWNER_COMMENT)
            echo(f"  This account can create {label} warehouses.")
        except ValueError:
            raise
        except Exception as exc:  # the connector raises many error types
            config = "gen2" if label == "Gen2" else "adaptive"
            others = " ".join(f"--config {c.key}" for c in queries.CONFIGS if c.key != config)
            echo(
                f"  This account can't create {label} warehouses ({exc}). {label} needs a region that has it"
                + (" and the Enterprise edition" if label == "Adaptive" else "")
                + f". To measure the others, pass {others} to `run`."
            )


def set_idle(cur: Any, warehouse: str) -> None:
    """Put the home warehouse back to X-Small with the idle timeout and its auto-suspend settings."""
    cur.execute(
        f"ALTER WAREHOUSE {warehouse} SET WAREHOUSE_SIZE = XSMALL "
        f"STATEMENT_TIMEOUT_IN_SECONDS = {queries.IDLE_TIMEOUT_SECONDS} AUTO_SUSPEND = 60 AUTO_RESUME = TRUE"
    )


def reset_to_idle(cur: Any, warehouse: str) -> None:
    """:func:`set_idle`, ignoring errors, for ``finally`` blocks that mustn't hide the real exception."""
    with suppress(Exception):
        set_idle(cur, warehouse)


def _check_account_usage(cur: Any, echo: Echo) -> None:
    """Warn up front about any ACCOUNT_USAGE view the role can't read. Only `report` needs them."""
    for view, sql in queries.ACCOUNT_USAGE_PROBES.items():
        try:
            cur.execute(sql)
            cur.fetchall()
        except Exception:  # any failure means the same thing here
            echo(
                f"  This role can't read SNOWFLAKE.ACCOUNT_USAGE.{view}. That's fine for `run`, but `report` "
                "needs it (ACCOUNTADMIN, or IMPORTED PRIVILEGES on the SNOWFLAKE database; QUERY_METERING_HISTORY "
                "needs the USAGE_VIEWER database role)."
            )
