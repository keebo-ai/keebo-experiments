"""The shared keebo-experiments root CLI."""

from __future__ import annotations

from click.testing import CliRunner

from common.cli import cli


def test_experiments_are_mounted():
    assert set(cli.commands) == {"warehouse-sizing", "multi-cluster-billing"}


def test_root_help_lists_experiments():
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "warehouse-sizing" in result.output
    assert "multi-cluster-billing" in result.output
