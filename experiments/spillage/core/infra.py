"""Create, check, and drop the demo's own Snowflake objects.

``setup`` creates:

- a dedicated **warehouse** (X-Small, generation pinned, suspended when idle), and
- a transient **database**, which gives the live-stats lookup a database to run
  in and, only if Snowflake's sample data isn't readable, holds a generated copy
  of the 60M-row ``LINEITEM`` table the workload reads,

each marked with :data:`queries.OWNER_COMMENT`. ``run`` and ``report`` refuse to
start unless both exist and carry that mark, and ``cleanup`` drops exactly them.
A same-named object without the mark is someone else's, and is never touched.
The only thing read from the account is the read-only sample-data share.

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
    """The names of the demo's warehouse and database. Build it with :meth:`named`, which validates them."""

    warehouse: str
    database: str

    @classmethod
    def named(cls, warehouse: str = queries.DEFAULT_WAREHOUSE, database: str = queries.DEFAULT_DATABASE) -> DemoObjects:
        return cls(validate_name(warehouse, "warehouse"), validate_name(database, "database"))

    @property
    def generated_table(self) -> str:
        """Where setup generates the source table when the sample data isn't readable."""
        return f"{self.database}.{queries.GENERATED_TABLE}"


@dataclass(frozen=True)
class DemoState:
    """What ``require`` found: the warehouse generation and the table the workload reads."""

    generation: str  # '1' or '2'
    source_table: str


def setup(conn: Any, objects: DemoObjects, *, generation: str = "1", echo: Echo = _silent) -> None:
    """Create the warehouse and database, and the source table if the sample data isn't readable. Safe to rerun."""
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

        # X-Small, with its own timeout, caps what generating the table can cost.
        cur.execute(
            f"ALTER WAREHOUSE {objects.warehouse} SET WAREHOUSE_SIZE = XSMALL "
            f"STATEMENT_TIMEOUT_IN_SECONDS = {queries.SETUP_TIMEOUT_SECONDS}"
        )
        cur.execute(f"USE WAREHOUSE {objects.warehouse}")
        try:
            if table_exists(cur, queries.SAMPLE_TABLE):
                echo(f"Using Snowflake's sample data, {queries.SAMPLE_TABLE} ({queries.SOURCE_ROWS:,} rows).")
            elif table_exists(cur, objects.generated_table):
                echo(f"Reusing table {objects.generated_table}.")
            else:
                echo(
                    f"This role can't read {queries.SAMPLE_TABLE}, so generating the same {queries.SOURCE_ROWS:,} "
                    f"rows as {objects.generated_table} instead (a minute or two) ..."
                )
                cur.execute(queries.GENERATED_TABLE_SQL.format(table=objects.generated_table, rows=queries.SOURCE_ROWS))
            _check_account_usage(cur, echo)
        finally:
            # Silent: the warehouse may never have resumed if everything already existed.
            suspend_quietly(cur, objects.warehouse)
        echo("\nReady. Next:  keebo-experiments spillage run --scenario local")
    finally:
        cur.close()


def require(cur: Any, objects: DemoObjects, *, echo: Echo = _silent) -> DemoState:
    """Check the demo objects exist and are ours; return the generation and the table to read.

    A generation the account doesn't report is taken as '2', the pricier one,
    so the cost cap errs on the safe side (and ``echo`` says so).
    """
    not_set_up = "Run `keebo-experiments spillage setup` first (with the same --warehouse / --database)."
    warehouse = dedicated.claim(cur, "WAREHOUSE", objects.warehouse, comment=queries.OWNER_COMMENT)
    if warehouse is None:
        raise ValueError(f"warehouse {objects.warehouse} doesn't exist. {not_set_up}")
    if dedicated.claim(cur, "DATABASE", objects.database, comment=queries.OWNER_COMMENT) is None:
        raise ValueError(f"database {objects.database} doesn't exist. {not_set_up}")
    source_table = _source_table(cur, objects)
    if source_table is None:
        raise ValueError(
            f"there's no table to read: this role can't read {queries.SAMPLE_TABLE}, and "
            f"{objects.generated_table} doesn't exist (setup didn't finish). {not_set_up}"
        )
    generation = warehouses.generation_of(warehouse)
    if generation is None:
        echo(f"Note: {objects.warehouse} doesn't report its generation, so costs assume Gen2 (the pricier rate).")
        generation = "2"
    return DemoState(generation=generation, source_table=source_table)


def _source_table(cur: Any, objects: DemoObjects) -> str | None:
    """The sample table if this role can read it, else the generated copy if setup made one."""
    for table in (queries.SAMPLE_TABLE, objects.generated_table):
        if table_exists(cur, table):
            return table
    return None


def table_exists(cur: Any, table: str) -> bool:
    """Whether ``DB.SCHEMA.NAME`` exists and this role can see it.

    For a database that's missing or not granted (e.g. an absent sample-data
    share) Snowflake raises rather than returning no rows; that counts as "no".
    """
    database, schema, name = table.split(".")
    try:
        cur.execute(f"SHOW TABLES LIKE '{name}' IN SCHEMA {database}.{schema}")
    except Exception:  # "does not exist or not authorized"
        return False
    columns = [column[0].lower() for column in cur.description]
    return any(dict(zip(columns, row, strict=False)).get("name") == name for row in cur.fetchall())


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
