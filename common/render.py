"""Render shared report types to the terminal.

The ``click``-aware counterpart to :mod:`common.tables`. Experiment CLI layers
call :func:`echo_table`; domain code stays free of ``click`` and just builds
:class:`~common.tables.ReportTable` values.
"""

from __future__ import annotations

import click

from common.tables import ReportTable


def echo_table(table: ReportTable) -> None:
    """Print one :class:`ReportTable` as an aligned, human-readable block."""
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
