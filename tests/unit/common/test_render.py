"""Shared table rendering."""

from __future__ import annotations

from common.render import echo_table, table_lines
from common.tables import ReportTable


def test_echo_table_prints_columns_and_rows(capsys):
    echo_table(ReportTable(step=1, title="Title", columns=["a", "b"], rows=[(1, 2)]))

    out = capsys.readouterr().out
    assert "Step 1. Title" in out
    assert "a" in out and "b" in out
    assert "1" in out and "2" in out


def test_echo_table_notes_when_empty(capsys):
    echo_table(ReportTable(step=2, title="Empty", columns=["a"], rows=[]))

    assert "no rows yet" in capsys.readouterr().out


def test_unnumbered_tables_print_just_the_title():
    lines = table_lines(ReportTable(None, "Live results", ["a"], [(1,)]))
    assert lines[1] == "--- Live results ---"
