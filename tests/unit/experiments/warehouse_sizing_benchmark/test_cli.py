"""CLI tests for the warehouse-sizing benchmark.

Uses click's ``CliRunner`` and ``mockito`` to stub the shared connection helper,
so the real command + domain code runs against a fake account. Command tests
use the ``--connection`` path to stay non-interactive.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from click.testing import CliRunner
from mockito import unstub, when

from experiments.warehouse_sizing_benchmark import cli as cli_module


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


def _invoke(runner, *args):
    return runner.invoke(cli_module.warehouse_sizing, [*args, "--connection", "test"])


def test_commands_are_registered():
    assert set(cli_module.warehouse_sizing.commands) == {"setup", "run", "report", "cleanup"}


@pytest.mark.parametrize("command", ["setup", "run", "report", "cleanup"])
def test_every_command_takes_the_object_names(runner, command):
    result = runner.invoke(cli_module.warehouse_sizing, [command, "--help"])
    assert result.exit_code == 0
    assert "--warehouse" in result.output and "--database" in result.output and "--connection" in result.output


def test_setup_pins_the_generation(runner, account):
    cursor, conn = account(warehouse_comment=None, database_comment=None)
    _stub_connection(conn)
    result = _invoke(runner, "setup", "--generation", "2")
    assert result.exit_code == 0, result.output
    assert any("GENERATION = '2'" in s for s in cursor.executed)


def test_run_two_sizes_prints_results_side_by_side(runner, account):
    cursor, conn = account()
    _stub_connection(conn)

    result = _invoke(runner, "run", "--size", "xsmall", "--size", "medium", "--runs", "1")

    assert result.exit_code == 0, result.output
    assert any("SET WAREHOUSE_SIZE = MEDIUM" in s for s in cursor.executed)
    assert "--- Results by size" in result.output
    assert "--- X-Small vs Medium ---" in result.output
    assert "This run used about" in result.output


def test_run_full_sweep_by_default(runner, account):
    cursor, conn = account()
    _stub_connection(conn)
    result = _invoke(runner, "run", "--runs", "1")
    assert result.exit_code == 0, result.output
    assert sum("SET WAREHOUSE_SIZE =" in s and "XSMALL STATEMENT" not in s for s in cursor.executed) >= 5
    assert " vs " not in result.output
    assert "Cheapest per query:" in result.output


def test_run_before_setup_is_a_clean_error(runner, account):
    _cursor, conn = account(warehouse_comment=None, database_comment=None)
    _stub_connection(conn)
    result = _invoke(runner, "run")
    assert result.exit_code != 0
    assert "warehouse-sizing setup" in result.output


def test_report_prints_every_step_of_the_latest_run(runner, account):
    _cursor, conn = account()
    _stub_connection(conn)
    result = _invoke(runner, "report")
    assert result.exit_code == 0, result.output
    assert "Reporting run 20260930-120000, the latest one ACCOUNT_USAGE has." in result.output
    assert "Step 10." in result.output and "Step 16." in result.output


def test_report_with_no_runs_yet(runner, account):
    _cursor, conn = account(latest_run=None)
    _stub_connection(conn)
    result = _invoke(runner, "report")
    assert result.exit_code == 0, result.output
    assert "doesn't show any runs on SIZING_BENCHMARK_WH" in result.output


def test_names_are_upper_cased(runner, account):
    cursor, conn = account()
    _stub_connection(conn)
    result = _invoke(runner, "report", "--warehouse", "sizing_benchmark_wh")
    assert result.exit_code == 0, result.output
    assert "USE WAREHOUSE SIZING_BENCHMARK_WH" in cursor.executed


def test_cleanup_drops_both_objects(runner, account):
    cursor, conn = account()
    _stub_connection(conn)
    result = runner.invoke(cli_module.warehouse_sizing, ["cleanup", "--connection", "test", "--yes"])
    assert result.exit_code == 0, result.output
    assert "DROP WAREHOUSE IF EXISTS SIZING_BENCHMARK_WH" in cursor.executed
    assert "DROP DATABASE IF EXISTS SIZING_BENCHMARK_DB" in cursor.executed
