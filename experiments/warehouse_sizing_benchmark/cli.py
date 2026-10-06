"""Warehouse-sizing benchmark — command-line front end.

Thin ``click`` wrappers over the domain layer in :mod:`core`. Mounted on the
shared ``keebo-experiments`` CLI (see :mod:`common.cli`) as the
``warehouse-sizing`` command group::

    poetry run keebo-experiments warehouse-sizing setup
    poetry run keebo-experiments warehouse-sizing run
    poetry run keebo-experiments warehouse-sizing report
    poetry run keebo-experiments warehouse-sizing cleanup

Credentials, connection opening, and table rendering are shared helpers in
``common`` so every experiment behaves identically.
"""

from __future__ import annotations

import click

from common import warehouses
from common.credentials import connection_option, open_connection
from common.render import echo_table
from experiments.warehouse_sizing_benchmark.core import infra, queries, sweep
from experiments.warehouse_sizing_benchmark.core import report as report_core

# Reused across commands, so their contracts never drift.
_WAREHOUSE_OPTION = click.option(
    "--warehouse",
    "warehouse_name",
    default=queries.DEFAULT_WAREHOUSE,
    show_default=True,
    help="The benchmark's own warehouse (created by setup, dropped by cleanup).",
)
_DATABASE_OPTION = click.option(
    "--database",
    default=queries.DEFAULT_DATABASE,
    show_default=True,
    help="The benchmark's own transient database (created by setup, dropped by cleanup).",
)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def warehouse_sizing() -> None:
    """Run the Keebo warehouse-sizing benchmark on your own Snowflake account.

    \b
    Typical flow:
        keebo-experiments warehouse-sizing setup     # create the warehouse and database
        keebo-experiments warehouse-sizing run       # run the query on every size
        keebo-experiments warehouse-sizing report    # billed numbers from ACCOUNT_USAGE (it lags, so wait a bit)
        keebo-experiments warehouse-sizing cleanup   # drop everything setup created

    \b
    To see what spill costs, compare two sizes (about 12 minutes, 0.25 credits):
        keebo-experiments warehouse-sizing run --size xsmall --size medium --runs 5

    Everything runs on a warehouse and database the benchmark creates, and it
    won't touch anything it didn't create. It reads Snowflake's sample data
    (TPCH_SF100.LINEITEM), or builds its own copy if your role can't read it.
    The role needs CREATE WAREHOUSE and CREATE DATABASE, plus ACCOUNT_USAGE
    access for report.

    Credentials: pass --connection NAME to use a connection from Snowflake's
    config, or set SNOWFLAKE_ACCOUNT / SNOWFLAKE_USER /
    SNOWFLAKE_PASSWORD (or SNOWFLAKE_AUTHENTICATOR) / SNOWFLAKE_ROLE in the
    environment or a .env file (see .env.example). With neither, your Snowflake
    default connection is used if you have one. Anything missing is prompted
    for.

    WARNING: this runs real queries and costs credits. A full sweep (X-Small to
    2X-Large) costs about 1.3 credits on TPCH_SF100. run stops at --max-credits
    (3 by default), including each size's 60-second minimum. setup costs about
    0.02 credits, or up to 0.5 if it has to build the table.
    """


@warehouse_sizing.command()
@_WAREHOUSE_OPTION
@_DATABASE_OPTION
@click.option(
    "--generation",
    type=click.Choice(warehouses.GENERATIONS),
    default="1",
    show_default=True,
    help="Warehouse generation. Gen2 costs 1.35x per hour, and the cost cap accounts for it.",
)
@connection_option
def setup(warehouse_name: str, database: str, generation: str, connection_name: str | None) -> None:
    """Create the benchmark warehouse and database (Steps 1 and 3). Safe to rerun."""
    try:
        objects = infra.BenchmarkObjects.named(warehouse_name, database)
        with open_connection(connection_name) as conn:
            infra.setup(conn, objects, generation=generation, echo=click.echo)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@warehouse_sizing.command()
@click.option(
    "--table",
    default=None,
    help=(
        "Fully-qualified table to query instead of the sample TPCH_SF100 (or setup's generated copy). "
        "TPCH_SF1000 spills more at every size and takes about 15x as long; raise --max-credits to 15 for a "
        "full sweep on it."
    ),
)
@click.option(
    "--size",
    "sizes",
    multiple=True,
    type=click.Choice(warehouses.SIZE_KEYWORDS, case_sensitive=False),
    help="Restrict the sweep to these sizes (repeatable). Defaults to all six. Pick two to see them side by side.",
)
@click.option(
    "--runs",
    default=3,
    show_default=True,
    type=click.IntRange(min=1),
    help="Runs per size. Run 1 is cold; later runs are warm.",
)
@click.option(
    "--max-credits",
    default=queries.DEFAULT_MAX_CREDITS,
    show_default=True,
    type=click.FloatRange(min=0.05, max=50),
    help="Stop the run at this many credits, including each size's 60-second minimum.",
)
@_WAREHOUSE_OPTION
@_DATABASE_OPTION
@connection_option
def run(
    table: str | None,
    sizes: tuple[str, ...],
    runs: int,
    max_credits: float,
    warehouse_name: str,
    database: str,
    connection_name: str | None,
) -> None:
    """Run the query on each size (Steps 2-9) and print the results when it's done."""
    selected = {size.upper() for size in sizes} if sizes else set(warehouses.SIZE_KEYWORDS)
    chosen_sizes = [keyword for keyword in warehouses.SIZE_KEYWORDS if keyword in selected]
    try:
        objects = infra.BenchmarkObjects.named(warehouse_name, database)
        with open_connection(connection_name) as conn:
            results = sweep.sweep_sizes(
                conn,
                objects=objects,
                table=table,
                sizes=chosen_sizes,
                runs=runs,
                max_credits=max_credits,
                echo=click.echo,
            )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    for result_table in report_core.live_tables(results):
        echo_table(result_table)
    for line in report_core.summary_lines(results):
        click.echo("\n" + line)


@warehouse_sizing.command()
@click.option(
    "--hours",
    default=24,
    show_default=True,
    type=click.IntRange(min=1),
    help="Lookback window for the ACCOUNT_USAGE queries.",
)
@click.option("--run-id", default=None, help="Which run to report (printed by `run`). Defaults to the latest.")
@_WAREHOUSE_OPTION
@_DATABASE_OPTION
@connection_option
def report(hours: int, run_id: str | None, warehouse_name: str, database: str, connection_name: str | None) -> None:
    """Read timings and credits back from ACCOUNT_USAGE for one run (Steps 10-16).

    Query history can take up to 45 minutes to show up, billing up to 3 hours,
    and per-query credits up to 8. Empty results mean it hasn't caught up yet.
    Wait and rerun.
    Run it before cleanup: it runs on the benchmark warehouse.
    """
    try:
        objects = infra.BenchmarkObjects.named(warehouse_name, database)
        with open_connection(connection_name) as conn:
            reported, tables = report_core.read_report(conn, objects=objects, hours=hours, run_id=run_id)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if reported is None:
        click.echo(
            f"ACCOUNT_USAGE doesn't show any runs on {objects.warehouse} in the last {hours} hours yet. "
            "It can take up to 45 minutes. Runs from before run ids aren't reported."
        )
        return
    if run_id is None:
        click.echo(
            f"Reporting run {reported}, the latest one ACCOUNT_USAGE has. If that's not the run you just did, "
            "give it a few minutes and try again, or pass --run-id."
        )
    else:
        click.echo(f"Run {reported}:")
    for table in tables:
        echo_table(table)


@warehouse_sizing.command()
@_WAREHOUSE_OPTION
@_DATABASE_OPTION
@connection_option
@click.confirmation_option(prompt="Drop the benchmark warehouse and database?")
def cleanup(warehouse_name: str, database: str, connection_name: str | None) -> None:
    """Drop the benchmark warehouse and database, and nothing else (Step 17)."""
    try:
        objects = infra.BenchmarkObjects.named(warehouse_name, database)
        with open_connection(connection_name) as conn:
            infra.cleanup(conn, objects, echo=click.echo)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
