"""CLI tests for the warehouse-sizing benchmark.

Uses click's ``CliRunner`` and ``mockito`` to stub the connection seam, so the
real command + domain code runs against a fake connection. Command tests use the
``--connection`` path to stay non-interactive; the env/prompt resolver is tested
directly.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from click.testing import CliRunner
from mockito import unstub, when

from common.snowflake import SnowflakeCredentials
from experiments.warehouse_sizing_benchmark import cli as cli_module


@pytest.fixture(autouse=True)
def _unstub():
    yield
    unstub()


@pytest.fixture
def runner():
    return CliRunner()


def _stub_connection(conn, name="test"):
    """Make ``--connection <name>`` resolve to ``conn``."""

    @contextmanager
    def _cm(connection_name):
        yield conn

    when(cli_module.sf).connection(connection_name=name).thenReturn(_cm(name))


def test_commands_are_registered():
    assert set(cli_module.cli.commands) == {"run", "report", "cleanup"}


def test_run_help_needs_no_credentials(runner):
    result = runner.invoke(cli_module.cli, ["run", "--help"])
    assert result.exit_code == 0
    assert "--table" in result.output
    assert "--connection" in result.output


def test_run_sweeps_against_the_connection(runner, make_cursor, make_connection):
    cursor = make_cursor(fetch=[("LINEITEM",)])
    _stub_connection(make_connection(cursor))

    result = runner.invoke(cli_module.cli, ["run", "--connection", "test", "--size", "xsmall", "--runs", "1"])

    assert result.exit_code == 0, result.output
    assert any("CREATE WAREHOUSE IF NOT EXISTS SIZING_BENCHMARK_WH" in s for s in cursor.executed)
    assert "ALTER WAREHOUSE SIZING_BENCHMARK_WH SET WAREHOUSE_SIZE = XSMALL" in cursor.executed


def test_report_prints_every_step(runner, make_cursor, make_connection):
    cursor = make_cursor(description=[("executions",)], fetch=[(18,)])
    _stub_connection(make_connection(cursor))

    result = runner.invoke(cli_module.cli, ["report", "--connection", "test"])

    assert result.exit_code == 0, result.output
    assert "Step 10." in result.output
    assert "Step 16." in result.output
    assert "executions" in result.output


def test_report_notes_when_account_usage_is_empty(runner, make_cursor, make_connection):
    cursor = make_cursor(description=[("executions",)], fetch=[])
    _stub_connection(make_connection(cursor))

    result = runner.invoke(cli_module.cli, ["report", "--connection", "test"])

    assert result.exit_code == 0, result.output
    assert "ACCOUNT_USAGE may still be catching up" in result.output


def test_cleanup_drops_the_warehouse(runner, make_cursor, make_connection):
    cursor = make_cursor()
    _stub_connection(make_connection(cursor))

    result = runner.invoke(cli_module.cli, ["cleanup", "--connection", "test", "--yes"])

    assert result.exit_code == 0, result.output
    assert "DROP WAREHOUSE IF EXISTS SIZING_BENCHMARK_WH" in cursor.executed


def test_resolve_credentials_from_env(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "acct")
    monkeypatch.setenv("SNOWFLAKE_USER", "user")
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "pw")
    monkeypatch.setenv("SNOWFLAKE_ROLE", "SYSADMIN")
    monkeypatch.delenv("SNOWFLAKE_AUTHENTICATOR", raising=False)

    creds = cli_module._resolve_credentials()

    assert creds == SnowflakeCredentials(account="acct", user="user", password="pw", role="SYSADMIN")


def test_resolve_credentials_prompts_for_missing(monkeypatch):
    for var in ("ACCOUNT", "USER", "PASSWORD", "ROLE", "AUTHENTICATOR"):
        monkeypatch.delenv(f"SNOWFLAKE_{var}", raising=False)
    answers = iter(["acct", "user", "pw"])  # account, user, password prompts
    monkeypatch.setattr(cli_module.click, "prompt", lambda *a, **k: next(answers))

    creds = cli_module._resolve_credentials()

    assert creds == SnowflakeCredentials(account="acct", user="user", password="pw", role=None)


def test_resolve_credentials_skips_password_prompt_with_sso(monkeypatch):
    for var in ("PASSWORD", "ROLE"):
        monkeypatch.delenv(f"SNOWFLAKE_{var}", raising=False)
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "acct")
    monkeypatch.setenv("SNOWFLAKE_USER", "user")
    monkeypatch.setenv("SNOWFLAKE_AUTHENTICATOR", "externalbrowser")

    def _no_prompt(*args, **kwargs):
        raise AssertionError("should not prompt when SSO is configured")

    monkeypatch.setattr(cli_module.click, "prompt", _no_prompt)

    creds = cli_module._resolve_credentials()

    assert creds == SnowflakeCredentials(account="acct", user="user", password=None, authenticator="externalbrowser")
