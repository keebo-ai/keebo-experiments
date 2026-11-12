"""Adaptive vs Gen1 vs Gen2 — command-line front end.

Thin ``click`` wrappers over the domain layer in :mod:`core`. Mounted on the
shared ``keebo-experiments`` CLI (see :mod:`common.cli`) as the
``adaptive-compute`` command group::

    poetry run keebo-experiments adaptive-compute setup
    poetry run keebo-experiments adaptive-compute run
    poetry run keebo-experiments adaptive-compute report
    poetry run keebo-experiments adaptive-compute cleanup

Credentials, connection opening, and table rendering are shared helpers in
``common`` so every experiment behaves identically.
"""

from __future__ import annotations

import csv
from pathlib import Path

import click

from common import warehouses
from common.credentials import connection_option, open_connection
from common.render import echo_table
from experiments.adaptive_compute.core import infra, plan, queries
from experiments.adaptive_compute.core import report as report_core
from experiments.adaptive_compute.core import run as run_core

# Reused across commands, so their contracts never drift.
_WAREHOUSE_OPTION = click.option(
    "--warehouse",
    "warehouse_name",
    default=queries.DEFAULT_WAREHOUSE,
    show_default=True,
    help="The experiment's home warehouse (created by setup, dropped by cleanup). Every cell's warehouse starts "
    "with this name.",
)
_DATABASE_OPTION = click.option(
    "--database",
    default=queries.DEFAULT_DATABASE,
    show_default=True,
    help="The experiment's own transient database (created by setup, dropped by cleanup).",
)
_SCALE_OPTION = click.option(
    "--scale",
    default=queries.DEFAULT_SCALE,
    show_default=True,
    type=click.Choice([str(s) for s in queries.SCALES]),
    callback=lambda _ctx, _param, value: int(value),
    help="TPC-H scale factor of the sample data to read (SF10's LINEITEM is 60M rows). Each step up is about 10x "
    "the work and the cost.",
)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def adaptive_compute() -> None:
    """Find where Snowflake's Adaptive warehouses save money, or time, against Gen1 and Gen2.

    \b
    Runs five workload shapes (one query, 22 TPC-H queries back to back, the
    same 22 all at once, a DML refresh, and two CTAS) on each size of each
    configuration, every one on a fresh warehouse of its own, and reads the bill
    back from ACCOUNT_USAGE. The report gives, for each workload and size, who's
    faster, who's cheaper, and two break-even points: how busy a standard
    warehouse must be to beat Adaptive, and the AUTO_SUSPEND above which
    Adaptive wins.

    \b
    Typical flow:
        keebo-experiments adaptive-compute setup     # home warehouse, database, LINEITEM copy; checks support
        keebo-experiments adaptive-compute run       # every cell (about 45 at the defaults)
        keebo-experiments adaptive-compute report    # bills and crossovers (ACCOUNT_USAGE lags up to 3 hours)
        keebo-experiments adaptive-compute cleanup   # drop everything the experiment created

    Everything runs on warehouses and a database the experiment creates and
    marks as its own, and it won't touch anything it didn't create. It reads
    Snowflake's free TPC-H sample data. The role needs CREATE WAREHOUSE and
    CREATE DATABASE, plus ACCOUNT_USAGE access for report. Adaptive needs the
    Enterprise edition and a region that has it.

    Credentials: pass --connection NAME to use a connection from Snowflake's
    config, or set SNOWFLAKE_ACCOUNT / SNOWFLAKE_USER /
    SNOWFLAKE_PASSWORD (or SNOWFLAKE_AUTHENTICATOR) / SNOWFLAKE_ROLE in the
    environment or a .env file (see .env.example). With neither, your Snowflake
    default connection is used if you have one. Anything missing is prompted
    for.

    WARNING: this uses real compute and costs credits. A default run (SF10,
    X-Small to Medium, 45 cells, about an hour) should cost roughly 2-4 credits;
    the 60-second minimums alone are about 1.4. run never exceeds --max-credits
    (10 by default), a worst case that assumes every warehouse runs flat out.
    setup costs under 0.05 credits at SF10.
    """


@adaptive_compute.command()
@_SCALE_OPTION
@_WAREHOUSE_OPTION
@_DATABASE_OPTION
@connection_option
def setup(scale: int, warehouse_name: str, database: str, connection_name: str | None) -> None:
    """Create the home warehouse and database, copy LINEITEM, and check Gen2 and Adaptive. Safe to rerun."""
    try:
        objects = infra.HomeObjects.named(warehouse_name, database)
        with open_connection(connection_name) as conn:
            infra.setup(conn, objects, scale=scale, echo=click.echo)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@adaptive_compute.command()
@click.option(
    "--config",
    "config_keys",
    multiple=True,
    type=click.Choice([c.key for c in queries.CONFIGS], case_sensitive=False),
    help="Configurations to measure (repeatable). Defaults to all three.",
)
@click.option(
    "--size",
    "sizes",
    multiple=True,
    type=click.Choice(warehouses.SIZE_KEYWORDS, case_sensitive=False),
    help=f"Sizes to measure (repeatable). For Adaptive, the MAX_QUERY_PERFORMANCE_LEVEL. Defaults to "
    f"{', '.join(queries.DEFAULT_SIZES)}.",
)
@click.option(
    "--workload",
    "workload_keys",
    multiple=True,
    type=click.Choice([w.key for w in queries.WORKLOADS], case_sensitive=False),
    help="Workloads to run (repeatable). Defaults to all five.",
)
@click.option(
    "--replicates",
    default=1,
    show_default=True,
    type=click.IntRange(min=1, max=5),
    help="Times to run the whole grid, each on fresh warehouses. Use 3 before quoting a number; it triples the cost.",
)
@click.option(
    "--max-clusters",
    default=queries.DEFAULT_MAX_CLUSTERS,
    show_default=True,
    type=click.IntRange(min=1, max=10),
    help="MAX_CLUSTER_COUNT of the standard warehouses for the concurrent workload. select.dev used 4. "
    "More than 1 needs the Enterprise edition.",
)
@click.option(
    "--throughput-multiplier",
    default=queries.DEFAULT_THROUGHPUT_MULTIPLIER,
    show_default=True,
    type=click.IntRange(min=0),
    help="Adaptive's QUERY_THROUGHPUT_MULTIPLIER: 0 (unlimited) or 2 and up. select.dev also tried 8.",
)
@click.option(
    "--max-credits",
    default=queries.DEFAULT_MAX_CREDITS,
    show_default=True,
    type=click.FloatRange(min=0.5, max=100),
    help="Never spend more than this. Each cell's wall-clock limit comes from it.",
)
@_SCALE_OPTION
@_WAREHOUSE_OPTION
@_DATABASE_OPTION
@connection_option
@click.option("--yes", is_flag=True, help="Skip the cost confirmation.")
def run(
    config_keys: tuple[str, ...],
    sizes: tuple[str, ...],
    workload_keys: tuple[str, ...],
    replicates: int,
    max_clusters: int,
    throughput_multiplier: int,
    max_credits: float,
    scale: int,
    warehouse_name: str,
    database: str,
    connection_name: str | None,
    yes: bool,
) -> None:
    """Run every cell of the grid, each on a fresh warehouse, and print the runtimes."""
    chosen_configs = {k.lower() for k in config_keys}
    chosen_workloads = {k.lower() for k in workload_keys}
    try:
        cells = plan.build_plan(
            configs=[c for c in queries.CONFIGS if not chosen_configs or c.key in chosen_configs],
            sizes=[s.upper() for s in sizes] or list(queries.DEFAULT_SIZES),
            workloads=[w for w in queries.WORKLOADS if not chosen_workloads or w.key in chosen_workloads],
            replicates=replicates,
        )
        summary = plan.summarize(
            cells, max_credits=max_credits, max_clusters=max_clusters, throughput_multiplier=throughput_multiplier
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if not yes:
        click.confirm(
            f"This creates {summary.cells} warehouses one at a time ({summary.standard_cells} standard, "
            f"{summary.adaptive_cells} Adaptive) and runs real queries on TPCH_SF{scale}. The standard warehouses' "
            f"60-second minimums alone come to {summary.minimum_credits:.2f} credits, and the run won't spend more "
            f"than {max_credits:g}. Continue?",
            abort=True,
        )

    try:
        objects = infra.HomeObjects.named(warehouse_name, database)
        with open_connection(connection_name) as conn:
            result = run_core.run_grid(
                conn,
                objects=objects,
                cells=cells,
                scale=scale,
                max_credits=max_credits,
                max_clusters=max_clusters,
                throughput_multiplier=throughput_multiplier,
                echo=click.echo,
            )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    for table in report_core.live_tables(result):
        echo_table(table)
    for line in report_core.live_summary_lines(result):
        click.echo("\n" + line)


@adaptive_compute.command()
@click.option(
    "--hours",
    default=48,
    show_default=True,
    type=click.IntRange(min=1),
    help="Lookback window for the ACCOUNT_USAGE queries.",
)
@click.option("--run-id", default=None, help="Which run to report (printed by `run`). Defaults to the latest.")
@click.option("--no-file", is_flag=True, help="Print the report without writing the CSV files.")
@_WAREHOUSE_OPTION
@_DATABASE_OPTION
@connection_option
def report(
    hours: int, run_id: str | None, no_file: bool, warehouse_name: str, database: str, connection_name: str | None
) -> None:
    """Read runtimes and bills back from ACCOUNT_USAGE, and say where the lines cross.

    Query history can take up to 45 minutes to show up, Adaptive's per-query
    bill up to an hour, and warehouse billing up to 3 hours. Missing numbers
    mean it hasn't caught up yet: wait and rerun. Unless --no-file, it also
    writes the cells and the comparisons to adaptive-compute-run-<id>.csv and
    adaptive-compute-run-<id>-crossovers.csv in this directory.
    """
    try:
        objects = infra.HomeObjects.named(warehouse_name, database)
        with open_connection(connection_name) as conn:
            summary = report_core.read_report(conn, objects=objects, hours=hours, run_id=run_id)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if summary is None:
        click.echo(
            f"ACCOUNT_USAGE doesn't show a run by {objects.warehouse} in the last {hours} hours yet. "
            "It can take up to 45 minutes."
        )
        return
    click.echo(f"Run {summary.run_id}" + ("" if run_id else " (the latest one ACCOUNT_USAGE has; pass --run-id)") + ":")
    for table in summary.tables:
        echo_table(table)
    click.echo("")
    for line in summary.lines:
        click.echo(line)
    if no_file:
        return
    cell_rows, comparison_rows = report_core.csv_rows(summary)
    for path, columns, data in (
        (Path(f"adaptive-compute-run-{summary.run_id}.csv"), report_core.CELL_CSV_COLUMNS, cell_rows),
        (
            Path(f"adaptive-compute-run-{summary.run_id}-crossovers.csv"),
            report_core.COMPARISON_CSV_COLUMNS,
            comparison_rows,
        ),
    ):
        with path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            writer.writerows(data)
        click.echo(f"Wrote {path}")


@adaptive_compute.command()
@_WAREHOUSE_OPTION
@_DATABASE_OPTION
@connection_option
@click.option(
    "--everything",
    is_flag=True,
    help="Also drop warehouses and databases the experiment created under other --warehouse / --database names.",
)
@click.confirmation_option(prompt="Drop the experiment's warehouses and database?")
def cleanup(warehouse_name: str, database: str, everything: bool, connection_name: str | None) -> None:
    """Drop everything the experiment created, and check nothing's left.

    That's the home warehouse, the database (with the LINEITEM copy and any
    scratch tables), and every cell or probe warehouse. Only objects carrying
    the experiment's owner comment are touched. Exits with an error if
    anything is still there afterward.
    """
    try:
        objects = infra.HomeObjects.named(warehouse_name, database)
        with open_connection(connection_name) as conn:
            remaining = infra.cleanup(conn, objects, everything=everything, echo=click.echo)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if remaining:
        raise click.ClickException(f"cleanup left {len(remaining)} object(s) behind; see above.")
