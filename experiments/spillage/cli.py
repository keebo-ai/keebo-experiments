"""Spillage demo — command-line front end.

Thin ``click`` wrappers over the domain layer in :mod:`core`. Mounted on the
shared ``keebo-experiments`` CLI (see :mod:`common.cli`) as the ``spillage``
command group::

    poetry run keebo-experiments spillage setup
    poetry run keebo-experiments spillage run --scenario local
    poetry run keebo-experiments spillage run --scenario remote
    poetry run keebo-experiments spillage report
    poetry run keebo-experiments spillage cleanup

Credentials, connection opening, and table rendering are shared helpers in
``common`` so every experiment behaves identically.
"""

from __future__ import annotations

import click

from common import warehouses
from common.credentials import connection_option, open_connection
from common.render import echo_table
from experiments.spillage.core import infra, queries
from experiments.spillage.core import report as report_core
from experiments.spillage.core import run as run_core

# Reused across commands, so their contracts never drift.
_WAREHOUSE_OPTION = click.option(
    "--warehouse",
    default=queries.DEFAULT_WAREHOUSE,
    show_default=True,
    help="The demo's own warehouse (created by setup, dropped by cleanup).",
)
_DATABASE_OPTION = click.option(
    "--database",
    default=queries.DEFAULT_DATABASE,
    show_default=True,
    help="The demo's own transient database (created by setup, dropped by cleanup).",
)
_SIZE_CHOICE = click.Choice(warehouses.SIZE_KEYWORDS, case_sensitive=False)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def spillage() -> None:
    """Show what disk spill costs you: the same workload on an undersized vs a right-sized warehouse.

    \b
    Two scenarios, both X-Small vs Medium:
        local   the undersized warehouse spills to local SSD
        remote  it spills past local SSD to remote storage

    \b
    Typical flow:
        keebo-experiments spillage setup                   # create the warehouse and database
        keebo-experiments spillage run --scenario local    # results print live
        keebo-experiments spillage run --scenario remote
        keebo-experiments spillage report                  # exact billed credits (wait a few min)
        keebo-experiments spillage cleanup                 # drop everything setup created

    Everything runs on a warehouse and database the demo creates and marks as
    its own; it never touches an object it didn't create. It sorts Snowflake's
    sample data (TPCH_SF10.LINEITEM), or a generated copy if the role can't
    read it. The role needs
    CREATE WAREHOUSE and CREATE DATABASE, plus ACCOUNT_USAGE access for report.

    Credentials: pass --connection NAME to use an entry from Snowflake's
    connections.toml, or set SNOWFLAKE_ACCOUNT / SNOWFLAKE_USER /
    SNOWFLAKE_PASSWORD (or SNOWFLAKE_AUTHENTICATOR) / SNOWFLAKE_ROLE in the
    environment or a .env file (see .env.example). Anything missing is prompted
    for.

    WARNING: this uses real compute. setup costs at most 0.25 credits (Gen1),
    and about 0.02 when the sample data is readable.
    Each run is capped at --max-credits (default 1.5) by a warehouse statement
    timeout that accounts for the warehouse generation, so setup plus both
    scenarios spend about 3.3 credits at most.
    """


@spillage.command()
@_WAREHOUSE_OPTION
@_DATABASE_OPTION
@click.option(
    "--generation",
    type=click.Choice(warehouses.GENERATIONS),
    default="1",
    show_default=True,
    help="Warehouse generation to pin. Gen2 bills 1.35x per hour; the cost cap accounts for it.",
)
@connection_option
def setup(warehouse: str, database: str, generation: str, connection_name: str | None) -> None:
    """Create the demo warehouse and database (and a 60M-row table if the sample data isn't readable)."""
    try:
        objects = infra.DemoObjects.named(warehouse, database)
        with open_connection(connection_name) as conn:
            infra.setup(conn, objects, generation=generation, echo=click.echo)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@spillage.command()
@click.option(
    "--scenario",
    type=click.Choice(sorted(queries.SCENARIOS), case_sensitive=False),
    default="local",
    show_default=True,
    help="local: spill to local SSD. remote: spill past local SSD to remote storage.",
)
@click.option(
    "--fanout",
    type=click.IntRange(min=1, max=100),
    default=None,
    help="Sort N x 60M rows (local defaults to 8, remote to 40). Higher = more spill and cost.",
)
@click.option("--undersized", type=_SIZE_CHOICE, default=None, help="Override the undersized warehouse size.")
@click.option("--right-sized", "right_sized", type=_SIZE_CHOICE, default=None, help="Override the right size.")
@click.option(
    "--max-credits",
    default=queries.DEFAULT_MAX_CREDITS,
    show_default=True,
    type=click.FloatRange(min=0.05, max=20),
    help="Hard cap on this run's compute credits, split evenly between the two sizes.",
)
@_WAREHOUSE_OPTION
@_DATABASE_OPTION
@connection_option
def run(
    scenario: str,
    fanout: int | None,
    undersized: str | None,
    right_sized: str | None,
    max_credits: float,
    warehouse: str,
    database: str,
    connection_name: str | None,
) -> None:
    """Run the workload on the undersized, then the right-sized warehouse, and compare."""
    try:
        objects = infra.DemoObjects.named(warehouse, database)
        chosen = queries.resolve_scenario(scenario, fanout=fanout, undersized=undersized, right_sized=right_sized)
        with open_connection(connection_name) as conn:
            results = run_core.run_comparison(
                conn, objects=objects, scenario=chosen, max_credits=max_credits, echo=click.echo
            )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    for table in report_core.comparison_tables(results, scenario=chosen):
        echo_table(table)


@spillage.command()
@click.option(
    "--hours",
    default=24,
    show_default=True,
    type=click.IntRange(min=1),
    help="Lookback window for the ACCOUNT_USAGE queries.",
)
@_WAREHOUSE_OPTION
@_DATABASE_OPTION
@connection_option
def report(hours: int, warehouse: str, database: str, connection_name: str | None) -> None:
    """Reconcile against ACCOUNT_USAGE: runtime, spill, and billed credits per run.

    ACCOUNT_USAGE lags a few minutes (up to ~45); QUERY_ATTRIBUTION_HISTORY can
    trail several hours. Empty results mean it hasn't caught up — wait and rerun.
    Run it before cleanup: it runs on the demo warehouse.
    """
    try:
        objects = infra.DemoObjects.named(warehouse, database)
        with open_connection(connection_name) as conn:
            tables = report_core.read_report(conn, objects=objects, hours=hours)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    for table in tables:
        echo_table(table)


@spillage.command()
@_WAREHOUSE_OPTION
@_DATABASE_OPTION
@connection_option
@click.confirmation_option(prompt="Drop the spillage demo warehouse and database?")
def cleanup(warehouse: str, database: str, connection_name: str | None) -> None:
    """Drop the demo warehouse and database, and nothing else."""
    try:
        objects = infra.DemoObjects.named(warehouse, database)
        with open_connection(connection_name) as conn:
            infra.cleanup(conn, objects, echo=click.echo)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
