"""Multi-cluster demo — command-line front end.

Mounted on the shared ``keebo-experiments`` CLI (see :mod:`common.cli`) as the
``multicluster-demo`` command group::

    poetry run keebo-experiments multicluster-demo run
    poetry run keebo-experiments multicluster-demo run --estimate   # cost only, no spend
    poetry run keebo-experiments multicluster-demo cleanup

This one SPENDS credits: it creates a temporary warehouse and runs real queries.
"""

from __future__ import annotations

import uuid

import click

from common.cost import estimate
from common.credentials import connection_option, open_connection, resolve_opener
from common.render import echo_table
from experiments.multicluster_demo.core import benchmark, queries, report

# Rough per-round upper bound (seconds) for the pre-run credit estimate.
_ROUND_SECONDS_ESTIMATE = 180

_WAREHOUSE_OPTION = click.option(
    "--warehouse",
    "warehouse_name",
    default=queries.DEFAULT_WAREHOUSE,
    show_default=True,
    help="Name of the dedicated demo warehouse (created and dropped by this command).",
)


def _estimate_credits(size: str, max_clusters: int) -> float:
    """Rough upper-bound credits: single-cluster round + multi-cluster round."""
    single = estimate(size, 1, _ROUND_SECONDS_ESTIMATE)
    multi = estimate(size, max_clusters, _ROUND_SECONDS_ESTIMATE)
    return single.credits + multi.credits


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def multicluster_demo() -> None:
    """Demonstrate what a multi-cluster warehouse does, and how you benefit.

    Runs the same batch of concurrent queries twice on a throwaway warehouse —
    once capped at a single cluster (queries queue) and once allowed to scale to
    several clusters (Snowflake spreads them out) — and compares queue time and
    total wall-clock.

    WARNING: this uses real compute. See `run --estimate` for the projected cost.
    The warehouse is dropped automatically when the run finishes.
    """


@multicluster_demo.command()
@_WAREHOUSE_OPTION
@click.option("--size", default=queries.DEFAULT_SIZE, show_default=True, help="Warehouse size.")
@click.option(
    "--concurrency",
    default=queries.DEFAULT_CONCURRENCY,
    show_default=True,
    type=click.IntRange(min=1),
    help="Number of genuinely concurrent queries (real sessions) per round.",
)
@click.option(
    "--max-clusters",
    default=queries.DEFAULT_MAX_CLUSTERS,
    show_default=True,
    type=click.IntRange(min=2),
    help="Cluster ceiling for the multi-cluster round.",
)
@click.option(
    "--table",
    default=queries.DEFAULT_TABLE,
    show_default=True,
    help="Fully-qualified table the workload query scans.",
)
@click.option("--estimate", "estimate_only", is_flag=True, help="Print the cost estimate and exit.")
@click.option("--yes", is_flag=True, help="Skip the cost confirmation prompt.")
@connection_option
def run(
    warehouse_name: str,
    size: str,
    concurrency: int,
    max_clusters: int,
    table: str,
    estimate_only: bool,
    yes: bool,
    connection_name: str | None,
) -> None:
    """Run the single-cluster vs multi-cluster comparison."""
    projected = _estimate_credits(size, max_clusters)
    click.echo(
        f"Estimated cost: up to ~{projected:.2f} credits "
        f"({size.upper()}, {concurrency} concurrent queries, 2 rounds, up to {max_clusters} clusters)."
    )
    if estimate_only:
        return
    if not yes:
        click.confirm("This creates a temporary warehouse and runs real queries. Proceed?", abort=True)

    opener = resolve_opener(connection_name)
    admin_conn = opener()
    try:
        single, multi = benchmark.run_demo(
            opener,
            admin_conn,
            warehouse=warehouse_name,
            size=size,
            concurrency=concurrency,
            max_clusters=max_clusters,
            table=table,
            run_token=f"mcdemo_{uuid.uuid4().hex[:8]}",
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        admin_conn.close()

    echo_table(report.comparison_table(single, multi))
    for line in report.takeaways(single, multi):
        click.echo(line)


@multicluster_demo.command()
@_WAREHOUSE_OPTION
@connection_option
@click.confirmation_option(prompt="Drop the demo warehouse?")
def cleanup(warehouse_name: str, connection_name: str | None) -> None:
    """Drop the demo warehouse (safety net if a run was interrupted)."""
    with open_connection(connection_name) as conn:
        queries.drop_warehouse(conn, warehouse_name)
    click.echo(f"Dropped {warehouse_name} (if it existed).")
