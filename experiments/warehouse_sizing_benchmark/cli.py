"""Warehouse-sizing benchmark — command-line front end.

Thin ``click`` wrappers over the domain layer in :mod:`core`. Mounted on the
shared ``keebo-experiments`` CLI (see :mod:`common.cli`) as the
``warehouse-sizing`` command group::

    poetry run keebo-experiments warehouse-sizing run
    poetry run keebo-experiments warehouse-sizing report
    poetry run keebo-experiments warehouse-sizing cleanup

Credentials, connection opening, and table rendering are shared helpers in
``common`` so every experiment behaves identically.
"""

from __future__ import annotations

import click

from common.credentials import connection_option, open_connection
from common.render import echo_table
from experiments.warehouse_sizing_benchmark.core import queries, sweep
from experiments.warehouse_sizing_benchmark.core import report as report_core

# Reused across commands, so their contracts never drift.
_WAREHOUSE_OPTION = click.option(
    "--warehouse",
    "warehouse_name",
    default=queries.DEFAULT_WAREHOUSE,
    show_default=True,
    help="The dedicated benchmark warehouse.",
)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def warehouse_sizing() -> None:
    """Run the Keebo warehouse-sizing benchmark on your own Snowflake account.

    \b
    Typical flow:
        keebo-experiments warehouse-sizing run       # create the warehouse and sweep sizes
        keebo-experiments warehouse-sizing report    # read timings + credits back (wait a few min)
        keebo-experiments warehouse-sizing cleanup   # drop the benchmark warehouse

    Credentials: pass --connection NAME to use an entry from Snowflake's
    connections.toml, or set SNOWFLAKE_ACCOUNT / SNOWFLAKE_USER /
    SNOWFLAKE_PASSWORD (or SNOWFLAKE_AUTHENTICATOR) / SNOWFLAKE_ROLE in the
    environment or a .env file (see .env.example). Anything missing is prompted
    for. SNOWFLAKE_ROLE needs ACCOUNT_USAGE access for the report.

    WARNING: this uses real compute. The full X-Small to 2X-Large sweep bills
    about 1.3 credits against TPCH_SF100.
    """


@warehouse_sizing.command()
@click.option(
    "--table",
    default=queries.DEFAULT_TABLE,
    show_default=True,
    help="Fully-qualified table to query. TPCH_SF1000 gives a sharper curve at ~10x cost.",
)
@_WAREHOUSE_OPTION
@click.option(
    "--size",
    "sizes",
    multiple=True,
    type=click.Choice(queries.SIZE_KEYWORDS, case_sensitive=False),
    help="Restrict the sweep to these sizes (repeatable). Defaults to all six.",
)
@click.option(
    "--runs",
    default=3,
    show_default=True,
    type=click.IntRange(min=1),
    help="Runs per size. Run 1 is cold; later runs are warm.",
)
@connection_option
def run(
    table: str,
    warehouse_name: str,
    sizes: tuple[str, ...],
    runs: int,
    connection_name: str | None,
) -> None:
    """Create the warehouse and run the fixed query across each size (Steps 1-9)."""
    selected = {size.upper() for size in sizes} if sizes else set(queries.SIZE_KEYWORDS)
    chosen_sizes = [row for row in queries.SIZES if row[0] in selected]
    try:
        with open_connection(connection_name) as conn:
            sweep.sweep_sizes(
                conn,
                table=table,
                warehouse_name=warehouse_name,
                sizes=chosen_sizes,
                runs=runs,
                echo=click.echo,
            )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@warehouse_sizing.command()
@_WAREHOUSE_OPTION
@click.option(
    "--hours",
    default=6,
    show_default=True,
    type=click.IntRange(min=1),
    help="Lookback window for the ACCOUNT_USAGE queries.",
)
@connection_option
def report(warehouse_name: str, hours: int, connection_name: str | None) -> None:
    """Read timings and credits back from ACCOUNT_USAGE (Steps 10-16).

    ACCOUNT_USAGE lags a few minutes (up to ~45); QUERY_ATTRIBUTION_HISTORY can
    trail several hours. Empty results mean it hasn't caught up — wait and rerun.
    """
    try:
        with open_connection(connection_name) as conn:
            tables = report_core.read_report(conn, warehouse_name=warehouse_name, hours=hours)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    for table in tables:
        echo_table(table)


@warehouse_sizing.command()
@_WAREHOUSE_OPTION
@connection_option
@click.confirmation_option(prompt="Drop the benchmark warehouse?")
def cleanup(warehouse_name: str, connection_name: str | None) -> None:
    """Drop the benchmark warehouse and nothing else (Step 17)."""
    try:
        with open_connection(connection_name) as conn:
            sweep.drop_warehouse(conn, warehouse_name=warehouse_name, echo=click.echo)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
