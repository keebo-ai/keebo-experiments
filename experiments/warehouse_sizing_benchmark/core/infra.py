"""Create, check, and drop the benchmark's own Snowflake objects (Steps 1, 3, and 17).

``setup`` creates:

- a dedicated **warehouse** (X-Small, generation pinned, suspended when idle), and
- a transient **database**, which gives the live-stats lookup a database to run
  in and, only if Snowflake's sample data isn't readable, holds a generated copy
  of the 600M-row ``LINEITEM`` table the benchmark reads,

each marked with :data:`queries.OWNER_COMMENT`. ``run`` and ``report`` refuse to
start unless both exist and carry that mark, and ``cleanup`` drops exactly them.
A same-named object without the mark is someone else's, and is never touched.
Every function is safe to rerun.

Functions take an open connection (or cursor). No ``click`` here; problems
raise ``ValueError`` and the CLI turns them into clean messages.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from common import dedicated, warehouses
from common.snowflake import is_statement_timeout, rows
from common.sql import validate_identifier, validate_name
from experiments.warehouse_sizing_benchmark.core import queries

Echo = Callable[[str], None]


def _silent(_message: str) -> None:
    """The default progress sink; the CLI passes ``click.echo`` instead."""


@dataclass(frozen=True)
class BenchmarkObjects:
    """The names of the benchmark's warehouse and database. Build it with :meth:`named`, which validates them."""

    warehouse: str
    database: str

    @classmethod
    def named(
        cls, warehouse: str = queries.DEFAULT_WAREHOUSE, database: str = queries.DEFAULT_DATABASE
    ) -> BenchmarkObjects:
        return cls(validate_name(warehouse, "warehouse"), validate_name(database, "database"))

    @property
    def generated_table(self) -> str:
        """Where setup generates the source table when the sample data isn't readable."""
        return f"{self.database}.{queries.GENERATED_TABLE}"


@dataclass(frozen=True)
class BenchmarkState:
    """What ``require`` found: the warehouse generation and the table to read."""

    generation: str  # '1' or '2'
    table: str


def setup(conn: Any, objects: BenchmarkObjects, *, generation: str = "1", echo: Echo = _silent) -> None:
    """Create the warehouse and database, and the table if the sample data isn't readable (Steps 1 and 3)."""
    if generation not in warehouses.GENERATIONS:
        raise ValueError(f"generation must be one of {', '.join(warehouses.GENERATIONS)}, got {generation!r}")
    cur = conn.cursor()
    try:
        cur.execute("SELECT CURRENT_ROLE()")
        echo(f"Using role {cur.fetchall()[0][0]} (it needs CREATE WAREHOUSE and CREATE DATABASE).")

        # Check both names before creating anything, so a name that's taken leaves nothing behind.
        warehouse_row = dedicated.claim(cur, "WAREHOUSE", objects.warehouse, comment=queries.OWNER_COMMENT)
        database_row = dedicated.claim(cur, "DATABASE", objects.database, comment=queries.OWNER_COMMENT)
        if warehouse_row is not None:
            reported = warehouses.generation_of(warehouse_row)
            if reported and reported != generation:
                raise ValueError(
                    f"warehouse {objects.warehouse} already exists as Gen{reported}. Run `cleanup` first, "
                    f"or rerun setup with --generation {reported}."
                )
            echo(f"Reusing warehouse {objects.warehouse}.")
        else:
            echo(f"Creating warehouse {objects.warehouse} (X-Small, Gen{generation}) ...")
            cur.execute(
                f"CREATE WAREHOUSE {objects.warehouse} "
                f"WAREHOUSE_SIZE = XSMALL GENERATION = '{generation}' "
                "AUTO_SUSPEND = 60 AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE "
                f"COMMENT = '{queries.OWNER_COMMENT}'"
            )

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
            if table_exists(cur, queries.DEFAULT_TABLE):
                echo(f"Using Snowflake's sample data, {queries.DEFAULT_TABLE} ({queries.SOURCE_ROWS:,} rows).")
            elif table_exists(cur, objects.generated_table):
                echo(f"Reusing table {objects.generated_table}.")
            else:
                _generate_table(cur, objects, echo)
            _check_account_usage(cur, echo)
        finally:
            # Silent: the warehouse may never have resumed if everything already existed.
            dedicated.suspend_quietly(cur, objects.warehouse)
        echo("\nReady. Next, run:  keebo-experiments warehouse-sizing run")
    finally:
        cur.close()


_NOT_SET_UP = "Run `keebo-experiments warehouse-sizing setup` first (with the same --warehouse / --database)."


def require(cur: Any, objects: BenchmarkObjects, *, table: str | None = None, echo: Echo = _silent) -> BenchmarkState:
    """Check the benchmark objects exist and are ours; return the generation and the table to read.

    ``table`` is an explicit ``--table``; without one, the sample data is read if
    it's readable, else the generated copy.
    """
    generation = require_warehouse(cur, objects, echo=echo)
    if dedicated.claim(cur, "DATABASE", objects.database, comment=queries.OWNER_COMMENT) is None:
        raise ValueError(f"database {objects.database} doesn't exist. {_NOT_SET_UP}")
    return BenchmarkState(generation=generation, table=_source_table(cur, objects, table))


def require_warehouse(cur: Any, objects: BenchmarkObjects, *, echo: Echo = _silent) -> str:
    """Check the warehouse exists and is ours; return its generation ('1' or '2').

    A generation the account doesn't report is taken as '2', the pricier one, so
    the cost cap errs on the safe side (and ``echo`` says so).
    """
    warehouse = dedicated.claim(cur, "WAREHOUSE", objects.warehouse, comment=queries.OWNER_COMMENT)
    if warehouse is None:
        raise ValueError(f"warehouse {objects.warehouse} doesn't exist. {_NOT_SET_UP}")
    generation = warehouses.generation_of(warehouse)
    if generation is None:
        echo(f"{objects.warehouse} doesn't report its generation, so costs are figured at the Gen2 rate to be safe.")
        return "2"
    return generation


def _source_table(cur: Any, objects: BenchmarkObjects, table: str | None) -> str:
    """The explicit ``--table`` if it's readable; else the sample data, else setup's generated copy."""
    if table is not None:
        validate_identifier(table, "table")
        if table.count(".") != 2:
            raise ValueError(f"--table must be fully qualified (DATABASE.SCHEMA.TABLE), got {table!r}")
        if not table_exists(cur, table):
            raise ValueError(f"table {table} doesn't exist, or this role can't read it.")
        return table
    source = next((t for t in (queries.DEFAULT_TABLE, objects.generated_table) if table_exists(cur, t)), None)
    if source is None:
        raise ValueError(
            f"there's no table to read: this role can't read {queries.DEFAULT_TABLE}, and "
            f"{objects.generated_table} doesn't exist (setup didn't finish). {_NOT_SET_UP}"
        )
    return source


def table_exists(cur: Any, table: str) -> bool:
    """Whether ``DB.SCHEMA.NAME`` exists and this role can see it.

    For a database that's missing or not granted (e.g. an absent sample-data
    share) Snowflake raises rather than returning no rows; that counts as "no".
    """
    database, schema, name = table.upper().split(".")
    try:
        cur.execute(f"SHOW TABLES LIKE '{name}' IN SCHEMA {database}.{schema}")
    except Exception:  # "does not exist or not authorized"
        return False
    return any(row.get("name") == name for row in rows(cur))


def cleanup(conn: Any, objects: BenchmarkObjects, *, echo: Echo = _silent) -> None:
    """Drop the benchmark warehouse and database, and nothing else (Step 17)."""
    cur = conn.cursor()
    try:
        for kind, name in (("WAREHOUSE", objects.warehouse), ("DATABASE", objects.database)):
            dropped = dedicated.drop(cur, kind, name, comment=queries.OWNER_COMMENT)
            echo(f"Dropped {kind.lower()} {name}." if dropped else f"No {kind.lower()} {name} to drop.")
    finally:
        cur.close()


def _generate_table(cur: Any, objects: BenchmarkObjects, echo: Echo) -> None:
    """Generate the fallback table on a Medium with its own cap, then go back to X-Small."""
    echo(
        f"This role can't read {queries.DEFAULT_TABLE}, so building {objects.generated_table} with the same "
        f"{queries.SOURCE_ROWS:,} rows instead. It takes up to {queries.GENERATE_TIMEOUT_SECONDS // 60} minutes "
        f"on a {warehouses.SIZE_LABEL[queries.GENERATE_SIZE]} ..."
    )
    cur.execute(
        f"ALTER WAREHOUSE {objects.warehouse} SET WAREHOUSE_SIZE = {queries.GENERATE_SIZE} "
        f"STATEMENT_TIMEOUT_IN_SECONDS = {queries.GENERATE_TIMEOUT_SECONDS}"
    )
    try:
        cur.execute(queries.GENERATED_TABLE_SQL.format(table=objects.generated_table, rows=queries.SOURCE_ROWS))
    except Exception as exc:
        if not is_statement_timeout(exc):
            raise
        raise ValueError(
            f"generating {objects.generated_table} hit its {queries.GENERATE_TIMEOUT_SECONDS}s cap, so nothing "
            "was created. Mount the sample data instead (see the README's Requirements), or pass --table to `run`."
        ) from exc
    finally:
        reset_to_idle(cur, objects.warehouse)


def set_idle(cur: Any, warehouse: str) -> None:
    """Put the warehouse back to X-Small with the idle timeout and its auto-suspend settings."""
    cur.execute(
        f"ALTER WAREHOUSE {warehouse} SET WAREHOUSE_SIZE = XSMALL "
        f"STATEMENT_TIMEOUT_IN_SECONDS = {queries.IDLE_TIMEOUT_SECONDS} AUTO_SUSPEND = 60 AUTO_RESUME = TRUE"
    )


def reset_to_idle(cur: Any, warehouse: str) -> None:
    """:func:`set_idle`, ignoring errors, for ``finally`` blocks that mustn't hide the real exception."""
    with suppress(Exception):
        set_idle(cur, warehouse)


def _check_account_usage(cur: Any, echo: Echo) -> None:
    """Warn up front if the role can't read ACCOUNT_USAGE, which only `report` needs."""
    try:
        cur.execute(queries.ACCOUNT_USAGE_PROBE)
        cur.fetchall()
    except Exception:  # any failure means the same thing here
        echo(
            "  This role can't read SNOWFLAKE.ACCOUNT_USAGE. That's fine for `run`, but `report` needs it "
            "(ACCOUNTADMIN, or IMPORTED PRIVILEGES on the SNOWFLAKE database)."
        )
