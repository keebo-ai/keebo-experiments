"""The single ``keebo-experiments`` CLI — one entry point for every experiment.

This is the composition root: it mounts each experiment's own command (defined
in ``experiments/<name>/cli.py``) as a subcommand, so the whole suite runs
through one console script::

    poetry run keebo-experiments <experiment> [options]

Dependencies flow one way. Experiment command modules import the shared helpers
in :mod:`common.credentials` and :mod:`common.render`; this root imports the
experiment commands. Experiments never import this module, so there is no cycle.
"""

from __future__ import annotations

import click

from experiments.multicluster_scaling.cli import multicluster_scaling
from experiments.warehouse_sizing_benchmark.cli import warehouse_sizing


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """Keebo Experiments: measure how your data warehouse really behaves.

    Each subcommand is a self-contained experiment. Start with the read-only
    ones (they spend no warehouse credits) before running any that use compute.
    """


cli.add_command(warehouse_sizing, "warehouse-sizing")
cli.add_command(multicluster_scaling, "multicluster-scaling")


if __name__ == "__main__":
    cli()
