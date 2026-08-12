"""CLI tests for the multicluster-demo experiment."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from experiments.multicluster_demo import cli as cli_module


@pytest.fixture
def runner():
    return CliRunner()


def test_commands_are_registered():
    assert set(cli_module.multicluster_demo.commands) == {"run", "cleanup"}


def test_run_help_lists_key_options(runner):
    result = runner.invoke(cli_module.multicluster_demo, ["run", "--help"])

    assert result.exit_code == 0
    assert "--concurrency" in result.output
    assert "--max-clusters" in result.output
    assert "--estimate" in result.output


def test_estimate_only_prints_cost_and_never_connects(runner, monkeypatch):
    calls = {"opener": 0}

    def _boom(*_args, **_kwargs):
        calls["opener"] += 1
        raise AssertionError("must not connect during --estimate")

    monkeypatch.setattr(cli_module, "resolve_opener", _boom)

    result = runner.invoke(cli_module.multicluster_demo, ["run", "--estimate"])

    assert result.exit_code == 0, result.output
    assert "Estimated cost" in result.output
    assert calls["opener"] == 0
