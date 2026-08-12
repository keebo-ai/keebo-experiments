"""Multi-cluster scaling experiment — command-line front end.

A thin ``click`` wrapper over :mod:`core`. Mounted on the shared
``keebo-experiments`` CLI (see :mod:`common.cli`) as the read-only
``multicluster-scaling`` command::

    poetry run keebo-experiments multicluster-scaling --days 14

Read-only: it only queries ``ACCOUNT_USAGE`` history and spends no warehouse
credits of its own.
"""

from __future__ import annotations

import click

from common.credentials import connection_option, open_connection
from common.render import echo_table
from experiments.multicluster_scaling.core import analysis, queries, report

DEFAULT_LOOKBACK_DAYS = 14


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--days",
    default=DEFAULT_LOOKBACK_DAYS,
    show_default=True,
    type=click.IntRange(min=1),
    help="How many days of history to analyze.",
)
@click.option(
    "--warehouse",
    "warehouse_names",
    multiple=True,
    metavar="NAME",
    help="Limit to these warehouse names (repeatable). Default: all warehouses.",
)
@click.option(
    "--run-warehouse",
    "run_warehouse",
    default=None,
    metavar="NAME",
    help="Warehouse to run the read-only ACCOUNT_USAGE queries on. Defaults to your role's default warehouse.",
)
@connection_option
def multicluster_scaling(
    days: int,
    warehouse_names: tuple[str, ...],
    run_warehouse: str | None,
    connection_name: str | None,
) -> None:
    """Show how Snowflake's multi-cluster scaling behaved on your warehouses (read-only, no credits).

    Reads warehouse-event and query history and reports, per warehouse, how many
    clusters ran, how often extra ones spun up, how long they lived, and how busy
    they were — so you can see how multi-cluster auto-scaling actually works on
    your own workload.
    """
    try:
        with open_connection(connection_name) as conn:
            if run_warehouse:
                queries.use_warehouse(conn, run_warehouse)
            events = queries.cluster_events(conn, days=days, warehouse_names=warehouse_names)
            history = queries.query_history(conn, days=days, warehouse_names=warehouse_names)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    scalings = analysis.summarize_scaling(events, history, warehouse_names or None)
    echo_table(report.summary_table(scalings, days=days))
    for line in report.behavior_notes(scalings):
        click.echo(line)
