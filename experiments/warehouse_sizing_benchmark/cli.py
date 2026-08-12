"""Warehouse-sizing benchmark — command-line front end.

Thin ``click`` wrappers over the domain layer in :mod:`core`. Each command
resolves Snowflake credentials from the environment, opens a connection, and
hands it to a domain function. Credentials are read from ``.env`` (or the real
environment); no secrets are passed as flags.

Installed as the ``warehouse-sizing-benchmark`` console script (see
``pyproject.toml``), so it runs as::

    poetry run warehouse-sizing-benchmark run
    poetry run warehouse-sizing-benchmark report
    poetry run warehouse-sizing-benchmark cleanup
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import click
from dotenv import load_dotenv

from common import snowflake as sf
from experiments.warehouse_sizing_benchmark.core import queries, sweep
from experiments.warehouse_sizing_benchmark.core import report as report_core

# Load .env once, so credentials can live in a file that git ignores.
load_dotenv()

# Reused across commands, so their contracts never drift.
_WAREHOUSE_OPTION = click.option(
    "--warehouse",
    "warehouse_name",
    default=queries.DEFAULT_WAREHOUSE,
    show_default=True,
    help="The dedicated benchmark warehouse.",
)


@contextmanager
def _connect() -> Iterator[Any]:
    """Open a connection from env credentials, as a context manager.

    Missing/invalid credentials surface as a clean ``click.ClickException``
    rather than a traceback.
    """
    try:
        creds = sf.credentials_from_env()
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    with sf.connection(creds) as conn:
        yield conn


def _echo_table(table: report_core.ReportTable) -> None:
    """Print one report step as an aligned table."""
    click.echo(f"\n--- Step {table.step}. {table.title} ---")
    if not table.rows:
        click.echo("  (no rows yet — ACCOUNT_USAGE may still be catching up)")
        return

    cells = [[("" if value is None else str(value)) for value in row] for row in table.rows]
    widths = [max(len(table.columns[i]), *(len(row[i]) for row in cells)) for i in range(len(table.columns))]
    click.echo("  " + "  ".join(name.ljust(widths[i]) for i, name in enumerate(table.columns)))
    click.echo("  " + "  ".join("-" * width for width in widths))
    for row in cells:
        click.echo("  " + "  ".join(row[i].ljust(widths[i]) for i in range(len(table.columns))))


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """Run the Keebo warehouse-sizing benchmark on your own Snowflake account.

    \b
    Typical flow:
        warehouse-sizing-benchmark run       # create the warehouse and sweep sizes
        warehouse-sizing-benchmark report    # read timings + credits back (wait a few min)
        warehouse-sizing-benchmark cleanup   # drop the benchmark warehouse

    Credentials come from the environment (or a .env file): SNOWFLAKE_ACCOUNT,
    SNOWFLAKE_USER, and SNOWFLAKE_PASSWORD (or SNOWFLAKE_AUTHENTICATOR); plus
    SNOWFLAKE_ROLE for the ACCOUNT_USAGE report. See .env.example.

    WARNING: this uses real compute. The full X-Small to 2X-Large sweep bills
    about 1.3 credits against TPCH_SF100.
    """


@cli.command()
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
def run(table: str, warehouse_name: str, sizes: tuple[str, ...], runs: int) -> None:
    """Create the warehouse and run the fixed query across each size (Steps 1-9)."""
    selected = {size.upper() for size in sizes} if sizes else set(queries.SIZE_KEYWORDS)
    chosen_sizes = [row for row in queries.SIZES if row[0] in selected]
    try:
        with _connect() as conn:
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


@cli.command()
@_WAREHOUSE_OPTION
@click.option(
    "--hours",
    default=6,
    show_default=True,
    type=click.IntRange(min=1),
    help="Lookback window for the ACCOUNT_USAGE queries.",
)
def report(warehouse_name: str, hours: int) -> None:
    """Read timings and credits back from ACCOUNT_USAGE (Steps 10-16).

    ACCOUNT_USAGE lags a few minutes (up to ~45); QUERY_ATTRIBUTION_HISTORY can
    trail several hours. Empty results mean it hasn't caught up — wait and rerun.
    """
    try:
        with _connect() as conn:
            tables = report_core.read_report(conn, warehouse_name=warehouse_name, hours=hours)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    for table in tables:
        _echo_table(table)


@cli.command()
@_WAREHOUSE_OPTION
@click.confirmation_option(prompt="Drop the benchmark warehouse?")
def cleanup(warehouse_name: str) -> None:
    """Drop the benchmark warehouse and nothing else (Step 17)."""
    try:
        with _connect() as conn:
            sweep.drop_warehouse(conn, warehouse_name=warehouse_name, echo=click.echo)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


if __name__ == "__main__":
    cli()
