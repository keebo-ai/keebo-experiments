"""Spillage demo — command-line front end.

Thin ``click`` wrappers over the domain layer in :mod:`core`. Mounted on the
shared ``keebo-experiments`` CLI (see :mod:`common.cli`) as the ``spillage``
command group::

    poetry run keebo-experiments spillage run --scenario local
    poetry run keebo-experiments spillage run --scenario remote
    poetry run keebo-experiments spillage report
    poetry run keebo-experiments spillage cleanup

Credentials, connection opening, and table rendering are shared helpers in
``common`` so every experiment behaves identically.
"""

from __future__ import annotations

import click

from common.credentials import connection_option, open_connection
from common.render import echo_table
from experiments.spillage.core import queries
from experiments.spillage.core import report as report_core
from experiments.spillage.core import run as run_core

# Reused across commands, so their contracts never drift.
_WAREHOUSE_OPTION = click.option(
    "--warehouse",
    "warehouse_name",
    default=queries.DEFAULT_WAREHOUSE,
    show_default=True,
    help="The dedicated demo warehouse.",
)
_SIZE_CHOICE = click.Choice(queries.SIZE_KEYWORDS, case_sensitive=False)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def spillage() -> None:
    """Show what disk spill costs you: the same workload on an undersized vs a right-sized warehouse.

    \b
    Two scenarios:
        local   the undersized warehouse spills to local SSD         (X-Small vs Medium)
        remote  it spills past local SSD to remote storage            (X-Small vs Medium)

    \b
    Typical flow:
        keebo-experiments spillage run --scenario local    # results print live
        keebo-experiments spillage run --scenario remote
        keebo-experiments spillage report                  # exact billed credits (wait a few min)
        keebo-experiments spillage cleanup                 # drop the demo warehouse

    Credentials: pass --connection NAME to use an entry from Snowflake's
    connections.toml, or set SNOWFLAKE_ACCOUNT / SNOWFLAKE_USER /
    SNOWFLAKE_PASSWORD (or SNOWFLAKE_AUTHENTICATOR) / SNOWFLAKE_ROLE in the
    environment or a .env file (see .env.example). Anything missing is prompted
    for. SNOWFLAKE_ROLE needs ACCOUNT_USAGE access for the report.

    WARNING: this uses real compute. Each `run` is capped at --max-credits
    (default 1.5) by a warehouse statement timeout, so both scenarios together
    spend about 3 credits at most.
    """


@spillage.command()
@click.option(
    "--scenario",
    type=click.Choice(sorted(queries.SCENARIOS), case_sensitive=False),
    default="local",
    show_default=True,
    help="local: spill to local SSD. remote: spill past local SSD to remote storage.",
)
@_WAREHOUSE_OPTION
@click.option("--table", default=None, help="Override the scenario's table (fully qualified).")
@click.option(
    "--fanout",
    type=click.IntRange(min=1, max=100),
    default=None,
    help="Sort N x the table's rows (local defaults to 8, remote to 40). Higher = more spill and cost.",
)
@click.option("--undersized", type=_SIZE_CHOICE, default=None, help="Override the undersized warehouse size.")
@click.option("--right-sized", "right_sized", type=_SIZE_CHOICE, default=None, help="Override the right size.")
@click.option(
    "--runs",
    default=1,
    show_default=True,
    type=click.IntRange(min=1),
    help="Runs per size. Run 1 (cold) is the one reported; later runs are warm.",
)
@click.option(
    "--max-credits",
    default=queries.DEFAULT_MAX_CREDITS,
    show_default=True,
    type=click.FloatRange(min=0.05, max=20),
    help="Hard cap on this run's compute credits, split evenly between the two sizes.",
)
@connection_option
def run(
    scenario: str,
    warehouse_name: str,
    table: str | None,
    fanout: int | None,
    undersized: str | None,
    right_sized: str | None,
    runs: int,
    max_credits: float,
    connection_name: str | None,
) -> None:
    """Run the workload on the undersized, then the right-sized warehouse, and compare."""
    try:
        chosen = queries.resolve_scenario(
            scenario, table=table, fanout=fanout, undersized=undersized, right_sized=right_sized
        )
        with open_connection(connection_name) as conn:
            results = run_core.run_comparison(
                conn,
                scenario=chosen,
                warehouse_name=warehouse_name,
                runs=runs,
                max_credits=max_credits,
                echo=click.echo,
            )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    for table_ in report_core.comparison_tables(results, scenario=chosen):
        echo_table(table_)


@spillage.command()
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
    """Reconcile against ACCOUNT_USAGE: runtime, spill, and billed credits.

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


@spillage.command()
@_WAREHOUSE_OPTION
@connection_option
@click.confirmation_option(prompt="Drop the spillage demo warehouse?")
def cleanup(warehouse_name: str, connection_name: str | None) -> None:
    """Drop the demo warehouse and nothing else."""
    try:
        with open_connection(connection_name) as conn:
            run_core.drop_warehouse(conn, warehouse_name=warehouse_name, echo=click.echo)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
