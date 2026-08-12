"""CLI tests for the warehouse-sizing benchmark.

Uses click's ``CliRunner`` and ``mockito`` to stub the connection seam, so the
real command + domain code runs against a fake connection.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from click.testing import CliRunner
from mockito import unstub, when

from common.snowflake import SnowflakeCredentials
from experiments.warehouse_sizing_benchmark import cli as cli_module

CREDS = SnowflakeCredentials(account="a", user="u", password="p")


@pytest.fixture(autouse=True)
def _unstub():
    yield
    unstub()


@pytest.fixture
def runner():
    return CliRunner()


def _stub_connection(conn):
    """Make the CLI's credential lookup and connection resolve to ``conn``."""

    @contextmanager
    def _cm(_creds):
        yield conn

    when(cli_module.sf).credentials_from_env().thenReturn(CREDS)
    when(cli_module.sf).connection(CREDS).thenReturn(_cm(CREDS))


def test_commands_are_registered():
    assert set(cli_module.cli.commands) == {"run", "report", "cleanup"}


def test_run_help_needs_no_credentials(runner):
    result = runner.invoke(cli_module.cli, ["run", "--help"])
    assert result.exit_code == 0
    assert "--table" in result.output
    assert "--runs" in result.output


def test_run_sweeps_against_the_connection(runner, make_cursor, make_connection):
    cursor = make_cursor(fetch=[("LINEITEM",)])
    _stub_connection(make_connection(cursor))

    result = runner.invoke(cli_module.cli, ["run", "--size", "xsmall", "--runs", "1"])

    assert result.exit_code == 0, result.output
    assert any("CREATE WAREHOUSE IF NOT EXISTS SIZING_BENCHMARK_WH" in s for s in cursor.executed)
    assert "ALTER WAREHOUSE SIZING_BENCHMARK_WH SET WAREHOUSE_SIZE = XSMALL" in cursor.executed


def test_report_prints_every_step(runner, make_cursor, make_connection):
    cursor = make_cursor(description=[("executions",)], fetch=[(18,)])
    _stub_connection(make_connection(cursor))

    result = runner.invoke(cli_module.cli, ["report"])

    assert result.exit_code == 0, result.output
    assert "Step 10." in result.output
    assert "Step 16." in result.output
    assert "executions" in result.output


def test_report_notes_when_account_usage_is_empty(runner, make_cursor, make_connection):
    cursor = make_cursor(description=[("executions",)], fetch=[])
    _stub_connection(make_connection(cursor))

    result = runner.invoke(cli_module.cli, ["report"])

    assert result.exit_code == 0, result.output
    assert "ACCOUNT_USAGE may still be catching up" in result.output


def test_cleanup_drops_the_warehouse(runner, make_cursor, make_connection):
    cursor = make_cursor()
    _stub_connection(make_connection(cursor))

    result = runner.invoke(cli_module.cli, ["cleanup", "--yes"])

    assert result.exit_code == 0, result.output
    assert "DROP WAREHOUSE IF EXISTS SIZING_BENCHMARK_WH" in cursor.executed


def test_missing_credentials_is_a_clean_error(runner):
    when(cli_module.sf).credentials_from_env().thenRaise(ValueError("SNOWFLAKE_ACCOUNT is not set"))

    result = runner.invoke(cli_module.cli, ["run", "--size", "xsmall", "--runs", "1"])

    assert result.exit_code != 0
    assert "SNOWFLAKE_ACCOUNT is not set" in result.output
