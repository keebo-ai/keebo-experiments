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
        keebo-experiments warehouse-sizing run       # sweep every size; results print live
        keebo-experiments warehouse-sizing report    # billed credits from ACCOUNT_USAGE (wait a few min)
        keebo-experiments warehouse-sizing cleanup   # drop everything setup created

    \b
    Compare just two sizes, e.g. to show what spill costs:
        keebo-experiments warehouse-sizing run --size xsmall --size medium --runs 1

    Everything runs on a warehouse and database the benchmark creates and marks
    as its own; it never touches an object it didn't create. It reads
    Snowflake's sample data (TPCH_SF100.LINEITEM), or a generated copy if the
    role can't read it. The role needs CREATE WAREHOUSE and CREATE DATABASE,
    plus ACCOUNT_USAGE access for report.

    Credentials: pass --connection NAME to use an entry from Snowflake's
    connections.toml, or set SNOWFLAKE_ACCOUNT / SNOWFLAKE_USER /
    SNOWFLAKE_PASSWORD (or SNOWFLAKE_AUTHENTICATOR) / SNOWFLAKE_ROLE in the
    environment or a .env file (see .env.example). Anything missing is prompted
    for.

    WARNING: this uses real compute. The full X-Small to 2X-Large sweep bills
    about 1.3 credits against TPCH_SF100. Every run is capped at --max-credits
    (default 3) by warehouse statement timeouts priced at the warehouse's
    generation. setup costs about 0.02 credits, or at most 0.5 if it has to
    generate the table.
    """


@warehouse_sizing.command()
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
def setup(warehouse_name: str, database: str, generation: str, connection_name: str | None) -> None:
    """Create the benchmark warehouse and database (Steps 1-3). Safe to rerun."""
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
        "Fully-qualified table to query instead of the sample TPCH_SF100 "
        "(or setup's generated copy). TPCH_SF1000 gives a sharper curve at ~10x cost."
    ),
)
@click.option(
    "--size",
    "sizes",
    multiple=True,
    type=click.Choice(queries.SIZE_KEYWORDS, case_sensitive=False),
    help="Restrict the sweep to these sizes (repeatable). Defaults to all six. Pick two for a side-by-side verdict.",
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
    help="Hard cap on this run's compute credits, split evenly across its queries.",
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
    """Run the fixed query across each size (Steps 4-9) and show the results live."""
    selected = {size.upper() for size in sizes} if sizes else set(queries.SIZE_KEYWORDS)
    chosen_sizes = [row for row in queries.SIZES if row[0] in selected]
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
    default=6,
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

    ACCOUNT_USAGE lags a few minutes (up to ~45); QUERY_ATTRIBUTION_HISTORY can
    trail several hours. Empty results mean it hasn't caught up — wait and rerun.
    Run it before cleanup: it runs on the benchmark warehouse.
    """
    try:
        objects = infra.BenchmarkObjects.named(warehouse_name, database)
        with open_connection(connection_name) as conn:
            reported, tables = report_core.read_report(conn, objects=objects, hours=hours, run_id=run_id)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if reported is None:
        click.echo(f"No benchmark runs on {objects.warehouse} in the last {hours} hours yet (ACCOUNT_USAGE may lag).")
        return
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
