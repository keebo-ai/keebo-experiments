"""Render shared report types to the terminal.

The ``click``-aware counterpart to :mod:`common.tables`. Experiment CLI layers
call :func:`echo_table`; domain code stays free of ``click`` and just builds
:class:`~common.tables.ReportTable` values.
"""

from __future__ import annotations

import click

from common.tables import ReportTable


def table_lines(table: ReportTable) -> list[str]:
    """One :class:`ReportTable` as aligned text lines (no I/O).

    Returned as lines so a caller can echo them to the terminal *or* fold them
    into a persisted report file, without the column-width math living in two
    places.
    """
    lines = ["", f"--- Step {table.step}. {table.title} ---"]
    if not table.rows:
        return [*lines, "  (no rows yet — ACCOUNT_USAGE may still be catching up)"]

    cells = [[("" if value is None else str(value)) for value in row] for row in table.rows]
    widths = [max(len(table.columns[i]), *(len(row[i]) for row in cells)) for i in range(len(table.columns))]
    lines.append("  " + "  ".join(name.ljust(widths[i]) for i, name in enumerate(table.columns)))
    lines.append("  " + "  ".join("-" * width for width in widths))
    lines.extend("  " + "  ".join(row[i].ljust(widths[i]) for i in range(len(table.columns))) for row in cells)
    return lines


def echo_table(table: ReportTable) -> None:
    """Print one :class:`ReportTable` as an aligned, human-readable block."""
    for line in table_lines(table):
        click.echo(line)
