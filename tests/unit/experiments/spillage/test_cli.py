"""CLI tests for the spillage demo.

Uses click's ``CliRunner`` and ``mockito`` to stub the shared connection helper,
so the real command + domain code runs against a fake connection. Command tests
use the ``--connection`` path to stay non-interactive.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from click.testing import CliRunner
from mockito import unstub, when

from experiments.spillage import cli as cli_module


@pytest.fixture(autouse=True)
def _unstub():
    yield
    unstub()


@pytest.fixture
def runner():
    return CliRunner()


def _stub_connection(conn, name="test"):
    """Make ``--connection <name>`` resolve to ``conn`` via the shared helper."""

    @contextmanager
    def _cm(connection_name):
        yield conn

    when(cli_module).open_connection(name).thenReturn(_cm(name))


def test_commands_are_registered():
    assert set(cli_module.spillage.commands) == {"run", "report", "cleanup"}


def test_run_help_needs_no_credentials(runner):
    result = runner.invoke(cli_module.spillage, ["run", "--help"])
    assert result.exit_code == 0
    assert "--scenario" in result.output
    assert "--fanout" in result.output
    assert "--connection" in result.output


def test_run_prints_the_comparison(runner, make_cursor, make_connection):
    cursor = make_cursor()
    _stub_connection(make_connection(cursor))

    result = runner.invoke(cli_module.spillage, ["run", "--connection", "test", "--scenario", "remote"])

    assert result.exit_code == 0, result.output
    assert "ALTER WAREHOUSE SPILLAGE_DEMO_WH SET WAREHOUSE_SIZE = MEDIUM" in cursor.executed
    assert any("GENERATOR(ROWCOUNT => 40)" in s for s in cursor.executed)
    assert "Step 1. Same workload, two warehouse sizes" in result.output
    assert "Step 2. The verdict" in result.output


def test_run_overrides_reach_the_warehouse(runner, make_cursor, make_connection):
    cursor = make_cursor()
    _stub_connection(make_connection(cursor))

    result = runner.invoke(
        cli_module.spillage,
        ["run", "--connection", "test", "--undersized", "small", "--right-sized", "medium", "--fanout", "2"],
    )

    assert result.exit_code == 0, result.output
    assert "ALTER WAREHOUSE SPILLAGE_DEMO_WH SET WAREHOUSE_SIZE = SMALL" in cursor.executed
    assert "ALTER WAREHOUSE SPILLAGE_DEMO_WH SET WAREHOUSE_SIZE = MEDIUM" in cursor.executed
    assert any("GENERATOR(ROWCOUNT => 2)" in s for s in cursor.executed)


def test_run_max_credits_sets_the_timeouts(runner, make_cursor, make_connection):
    cursor = make_cursor()
    _stub_connection(make_connection(cursor))

    result = runner.invoke(cli_module.spillage, ["run", "--connection", "test", "--max-credits", "0.5"])

    assert result.exit_code == 0, result.output
    assert "Cost cap: at most 0.5 credits" in result.output
    assert "ALTER WAREHOUSE SPILLAGE_DEMO_WH SET STATEMENT_TIMEOUT_IN_SECONDS = 900" in cursor.executed
    assert "ALTER WAREHOUSE SPILLAGE_DEMO_WH SET STATEMENT_TIMEOUT_IN_SECONDS = 225" in cursor.executed


def test_run_rejects_a_budget_below_the_billing_minimum(runner, make_cursor, make_connection):
    _stub_connection(make_connection(make_cursor()))

    result = runner.invoke(cli_module.spillage, ["run", "--connection", "test", "--max-credits", "0.1"])

    assert result.exit_code != 0
    assert "under 60 seconds" in result.output


def test_run_bad_table_is_a_clean_error(runner, make_cursor, make_connection):
    _stub_connection(make_connection(make_cursor()))

    result = runner.invoke(cli_module.spillage, ["run", "--connection", "test", "--table", "bad;table"])

    assert result.exit_code != 0
    assert "table must match" in result.output


def test_report_prints_every_step(runner, make_cursor, make_connection):
    cursor = make_cursor(description=[("query_tag",)], fetch=[("spill:local:undersized:1",)])
    _stub_connection(make_connection(cursor))

    result = runner.invoke(cli_module.spillage, ["report", "--connection", "test"])

    assert result.exit_code == 0, result.output
    assert "Step 1." in result.output
    assert "Step 3." in result.output


def test_cleanup_drops_the_warehouse(runner, make_cursor, make_connection):
    cursor = make_cursor()
    _stub_connection(make_connection(cursor))

    result = runner.invoke(cli_module.spillage, ["cleanup", "--connection", "test", "--yes"])

    assert result.exit_code == 0, result.output
    assert "DROP WAREHOUSE IF EXISTS SPILLAGE_DEMO_WH" in cursor.executed
