"""Create, check, and drop the demo's own Snowflake objects.

The demo borrows nothing from the account. ``setup`` creates:

- a dedicated **warehouse** (X-Small, generation pinned, suspended when idle), and
- a transient **database** holding a generated 60M-row ``LINEITEM`` table,

each marked with :data:`queries.OWNER_COMMENT`. ``run`` and ``report`` refuse to
start unless both exist and carry that mark, and ``cleanup`` drops exactly them.
A same-named object without the mark is someone else's, and is never touched.

Functions take an open connection (or cursor). No ``click`` here; problems
raise ``ValueError`` and the CLI turns them into clean messages.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from common import dedicated, warehouses
from common.sql import validate_name
from experiments.spillage.core import queries

Echo = Callable[[str], None]


def _silent(_message: str) -> None:
    """The default progress sink; the CLI passes ``click.echo`` instead."""


@dataclass(frozen=True)
class DemoObjects:
    """The names of the demo's warehouse and database (validated, upper-cased)."""

    warehouse: str
    database: str

    @classmethod
    def named(cls, warehouse: str = queries.DEFAULT_WAREHOUSE, database: str = queries.DEFAULT_DATABASE) -> DemoObjects:
        return cls(validate_name(warehouse, "warehouse"), validate_name(database, "database"))

    @property
    def table(self) -> str:
        return f"{self.database}.{queries.SOURCE_TABLE}"


def setup(conn: Any, objects: DemoObjects, *, generation: str = "1", echo: Echo = _silent) -> None:
    """Create the warehouse, database, and source table. Safe to rerun."""
    if generation not in warehouses.GENERATIONS:
        raise ValueError(f"generation must be one of {', '.join(warehouses.GENERATIONS)}, got {generation!r}")
    cur = conn.cursor()
    try:
        cur.execute("SELECT CURRENT_ROLE()")
        echo(f"Using role {cur.fetchall()[0][0]} (it needs CREATE WAREHOUSE and CREATE DATABASE).")

        existing = dedicated.claim(cur, "WAREHOUSE", objects.warehouse, comment=queries.OWNER_COMMENT)
        if existing is None:
            echo(f"Creating warehouse {objects.warehouse} (X-Small, Gen{generation}) ...")
            cur.execute(
                f"CREATE WAREHOUSE {objects.warehouse} "
                f"WAREHOUSE_SIZE = XSMALL GENERATION = '{generation}' "
                "AUTO_SUSPEND = 60 AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE "
                f"COMMENT = '{queries.OWNER_COMMENT}'"
            )
        else:
            reported = warehouses.generation_of(existing)
            if reported and reported != generation:
                raise ValueError(
                    f"warehouse {objects.warehouse} already exists as Gen{reported}. Run `cleanup` first, "
                    f"or rerun setup with --generation {reported}."
                )
            echo(f"Reusing warehouse {objects.warehouse}.")

        if dedicated.claim(cur, "DATABASE", objects.database, comment=queries.OWNER_COMMENT) is None:
            echo(f"Creating database {objects.database} (transient: no Time Travel or Fail-safe storage) ...")
            cur.execute(
                f"CREATE TRANSIENT DATABASE {objects.database} "
                f"DATA_RETENTION_TIME_IN_DAYS = 0 COMMENT = '{queries.OWNER_COMMENT}'"
            )
        else:
            echo(f"Reusing database {objects.database}.")

        echo(f"Generating {objects.table} ({queries.SOURCE_ROWS:,} rows; skipped if it already exists) ...")
        # X-Small, with its own timeout, caps what generating the table can cost.
        cur.execute(
            f"ALTER WAREHOUSE {objects.warehouse} SET WAREHOUSE_SIZE = XSMALL "
            f"STATEMENT_TIMEOUT_IN_SECONDS = {queries.SETUP_TIMEOUT_SECONDS}"
        )
        cur.execute(f"USE WAREHOUSE {objects.warehouse}")
        try:
            cur.execute(queries.SOURCE_TABLE_SQL.format(table=objects.table, rows=queries.SOURCE_ROWS))
            _check_account_usage(cur, echo)
        finally:
            # Silent: on a rerun the table already exists and the warehouse may never have resumed.
            suspend_quietly(cur, objects.warehouse)
        echo("\nReady. Next:  keebo-experiments spillage run --scenario local")
    finally:
        cur.close()


def require(cur: Any, objects: DemoObjects) -> str:
    """Check the demo objects exist and are ours; return the warehouse generation ('1' / '2').

    A generation the account doesn't report is taken as '2', the pricier one,
    so the cost cap errs on the safe side.
    """
    not_set_up = "Run `keebo-experiments spillage setup` first (with the same --warehouse / --database)."
    warehouse = dedicated.claim(cur, "WAREHOUSE", objects.warehouse, comment=queries.OWNER_COMMENT)
    if warehouse is None:
        raise ValueError(f"warehouse {objects.warehouse} doesn't exist. {not_set_up}")
    if dedicated.claim(cur, "DATABASE", objects.database, comment=queries.OWNER_COMMENT) is None:
        raise ValueError(f"database {objects.database} doesn't exist. {not_set_up}")
    return warehouses.generation_of(warehouse) or "2"


def cleanup(conn: Any, objects: DemoObjects, *, echo: Echo = _silent) -> None:
    """Drop the demo warehouse and database, and nothing else."""
    cur = conn.cursor()
    try:
        for kind, name in (("WAREHOUSE", objects.warehouse), ("DATABASE", objects.database)):
            dropped = dedicated.drop(cur, kind, name, comment=queries.OWNER_COMMENT)
            echo(f"Dropped {kind.lower()} {name}." if dropped else f"No {kind.lower()} {name} to drop.")
    finally:
        cur.close()


def suspend_quietly(cur: Any, warehouse: str, echo: Echo = _silent) -> None:
    """Suspend ``warehouse``, ignoring errors.

    Used in ``finally`` blocks, where a failure (e.g. "already suspended")
    would otherwise hide the real exception. AUTO_SUSPEND = 60 is the backstop.
    """
    try:
        cur.execute(f"ALTER WAREHOUSE {warehouse} SUSPEND")
    except Exception as exc:  # best effort by design
        echo(f"  (couldn't suspend {warehouse}: {exc}; it auto-suspends after 60s idle)")


def _check_account_usage(cur: Any, echo: Echo) -> None:
    """Warn up front if the role can't read ACCOUNT_USAGE, which only `report` needs."""
    try:
        cur.execute(queries.ACCOUNT_USAGE_PROBE)
        cur.fetchall()
    except Exception:  # any failure means the same thing here
        echo(
            "  Note: this role can't read SNOWFLAKE.ACCOUNT_USAGE. `run` works without it; "
            "`report` needs it (ACCOUNTADMIN, or IMPORTED PRIVILEGES on the SNOWFLAKE database)."
        )
