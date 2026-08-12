"""Shared table rendering."""

from __future__ import annotations

from common.render import echo_table
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
