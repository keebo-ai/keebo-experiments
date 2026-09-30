"""CLI tests for the spillage demo.

Uses click's ``CliRunner`` and ``mockito`` to stub the shared connection helper,
so the real command + domain code runs against a fake account. Command tests
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


def _invoke(runner, *args):
    return runner.invoke(cli_module.spillage, [*args, "--connection", "test"])


def test_commands_are_registered():
    assert set(cli_module.spillage.commands) == {"setup", "run", "report", "cleanup"}


@pytest.mark.parametrize("command", ["setup", "run", "report", "cleanup"])
def test_every_command_takes_the_demo_object_names(runner, command):
    result = runner.invoke(cli_module.spillage, [command, "--help"])
    assert result.exit_code == 0
    assert "--warehouse" in result.output
    assert "--database" in result.output
    assert "--connection" in result.output


def test_setup_pins_the_requested_generation(runner, account):
    cursor, conn = account(warehouse_comment=None, database_comment=None)
    _stub_connection(conn)

    result = _invoke(runner, "setup", "--generation", "2")

    assert result.exit_code == 0, result.output
    assert any("GENERATION = '2'" in s for s in cursor.executed)
    assert "Ready." in result.output


def test_run_prints_the_comparison(runner, account):
    cursor, conn = account()
    _stub_connection(conn)

    result = _invoke(runner, "run", "--scenario", "remote")

    assert result.exit_code == 0, result.output
    assert any("GENERATOR(ROWCOUNT => 40)" in s for s in cursor.executed)
    assert "Step 1. Same workload, two warehouse sizes — Local + remote spill" in result.output
    assert "Step 2. The verdict" in result.output


def test_run_overrides_reach_the_warehouse(runner, account):
    cursor, conn = account()
    _stub_connection(conn)

    result = _invoke(runner, "run", "--undersized", "small", "--right-sized", "large", "--max-credits", "3")

    assert result.exit_code == 0, result.output
    assert "ALTER WAREHOUSE SPILLAGE_DEMO_WH SET WAREHOUSE_SIZE = SMALL STATEMENT_TIMEOUT_IN_SECONDS = 2700" in (
        cursor.executed
    )
    assert "ALTER WAREHOUSE SPILLAGE_DEMO_WH SET WAREHOUSE_SIZE = LARGE STATEMENT_TIMEOUT_IN_SECONDS = 675" in (
        cursor.executed
    )


def test_names_are_upper_cased_everywhere(runner, account):
    cursor, conn = account()
    _stub_connection(conn)

    result = _invoke(runner, "report", "--warehouse", "spillage_demo_wh")

    assert result.exit_code == 0, result.output
    assert "USE WAREHOUSE SPILLAGE_DEMO_WH" in cursor.executed


def test_bad_names_are_clean_errors(runner):
    result = runner.invoke(cli_module.spillage, ["run", "--warehouse", "bad;name"])
    assert result.exit_code != 0
    assert "warehouse must be a single unquoted name" in result.output


def test_run_before_setup_is_a_clean_error(runner, account):
    _cursor, conn = account(warehouse_comment=None, database_comment=None)
    _stub_connection(conn)

    result = _invoke(runner, "run")

    assert result.exit_code != 0
    assert "spillage setup" in result.output


def test_run_rejects_a_budget_below_the_billing_minimum(runner, account):
    _cursor, conn = account()
    _stub_connection(conn)

    result = _invoke(runner, "run", "--max-credits", "0.1")

    assert result.exit_code != 0
    assert "under 60 seconds" in result.output


def test_report_prints_every_step(runner, account):
    _cursor, conn = account()
    _stub_connection(conn)

    result = _invoke(runner, "report")

    assert result.exit_code == 0, result.output
    assert "Step 1." in result.output and "Step 3." in result.output


def test_cleanup_drops_both_objects(runner, account):
    cursor, conn = account()
    _stub_connection(conn)

    result = runner.invoke(cli_module.spillage, ["cleanup", "--connection", "test", "--yes"])

    assert result.exit_code == 0, result.output
    assert "DROP WAREHOUSE IF EXISTS SPILLAGE_DEMO_WH" in cursor.executed
    assert "DROP DATABASE IF EXISTS SPILLAGE_DEMO_DB" in cursor.executed


def test_cleanup_refuses_someone_elses_warehouse(runner, account):
    cursor, conn = account(warehouse_comment="production")
    _stub_connection(conn)

    result = runner.invoke(cli_module.spillage, ["cleanup", "--connection", "test", "--yes"])

    assert result.exit_code != 0
    assert "wasn't created by this experiment" in result.output
    assert not any(s.startswith("DROP") for s in cursor.executed)
