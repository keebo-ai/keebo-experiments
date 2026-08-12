"""CLI tests for the multicluster-scaling experiment."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from click.testing import CliRunner
from mockito import unstub, when

from experiments.multicluster_scaling import cli as cli_module


@pytest.fixture(autouse=True)
def _unstub():
    yield
    unstub()


@pytest.fixture
def runner():
    return CliRunner()


def _stub_connection(conn, name="test"):
    @contextmanager
    def _cm(connection_name):
        yield conn

    when(cli_module).open_connection(name).thenReturn(_cm(name))


def test_help_needs_no_credentials(runner):
    result = runner.invoke(cli_module.multicluster_scaling, ["--help"])

    assert result.exit_code == 0
    assert "--days" in result.output
    assert "--warehouse" in result.output


def test_empty_history_reports_no_activity(runner, make_cursor, make_connection):
    _stub_connection(make_connection(make_cursor(fetch=[])))

    result = runner.invoke(cli_module.multicluster_scaling, ["--connection", "test"])

    assert result.exit_code == 0, result.output
    assert "How multi-cluster scaling behaved" in result.output
    assert "No multi-cluster warehouses" in result.output


def test_dashed_warehouse_name_is_accepted(runner, make_cursor, make_connection):
    _stub_connection(make_connection(make_cursor(fetch=[])))

    # Hyphenated/lower-case warehouse names are valid quoted identifiers. An
    # explicitly-named warehouse always gets a row, even with no activity.
    result = runner.invoke(
        cli_module.multicluster_scaling,
        ["--connection", "test", "--warehouse", "some-dashed-wh"],
    )

    assert result.exit_code == 0, result.output
    assert "some-dashed-wh" in result.output
    assert "Never scaled past its base cluster" in result.output
