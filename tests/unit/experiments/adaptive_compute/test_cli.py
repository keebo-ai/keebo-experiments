"""CLI tests for the adaptive-compute experiment.

Uses click's ``CliRunner`` and ``mockito`` to stub the shared connection helper,
so the real command + domain code runs against a fake account. Command tests
use the ``--connection`` path to stay non-interactive.
"""

from __future__ import annotations

import csv
from contextlib import contextmanager

import pytest
from click.testing import CliRunner
from mockito import unstub, when

from experiments.adaptive_compute import cli as cli_module
from experiments.adaptive_compute.core import report as report_core

from .conftest import OURS, RUN_ID, cell_row


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


def _invoke(runner, *args, input=None):
    return runner.invoke(cli_module.adaptive_compute, [*args, "--connection", "test"], input=input)


def test_commands_are_registered():
    assert set(cli_module.adaptive_compute.commands) == {"setup", "run", "report", "cleanup"}


@pytest.mark.parametrize("command", ["setup", "run", "report", "cleanup"])
def test_every_command_takes_the_object_names(runner, command):
    result = runner.invoke(cli_module.adaptive_compute, [command, "--help"])
    assert result.exit_code == 0
    assert "--warehouse" in result.output and "--database" in result.output and "--connection" in result.output


def test_group_help_carries_the_cost_warning(runner):
    result = runner.invoke(cli_module.adaptive_compute, ["--help"])
    assert "WARNING: this uses real compute" in result.output


def test_setup(runner, account):
    cursor, conn, _fake = account(warehouses={}, databases={}, base_table=False)
    _stub_connection(conn)
    result = _invoke(runner, "setup", "--scale", "1")
    assert result.exit_code == 0, result.output
    assert any("TPCH_SF1.LINEITEM ORDER BY l_shipdate" in s for s in cursor.executed)


def test_run_asks_before_spending_anything(runner, account):
    cursor, conn, _fake = account()
    _stub_connection(conn)

    result = _invoke(runner, "run", "--workload", "single", input="n\n")

    assert result.exit_code == 1
    assert "creates 9 warehouses" in result.output and "won't spend more than 10" in result.output
    assert cursor.executed == []


def test_run_prints_runtimes_side_by_side(runner, account):
    cursor, conn, fake = account()
    _stub_connection(conn)

    result = _invoke(runner, "run", "--workload", "single", "--size", "xsmall", "--yes")

    assert result.exit_code == 0, result.output
    assert sum(s.startswith("CREATE WAREHOUSE") for s in cursor.executed) == 3
    assert "--- Runtime side by side (median wall seconds) ---" in result.output
    assert "adaptive-compute report --run-id" in result.output
    assert list(fake.warehouses) == ["ADAPTIVE_COMPUTE_WH"]


def test_run_subsets_the_grid(runner, account):
    cursor, conn, _fake = account()
    _stub_connection(conn)
    result = _invoke(runner, "run", "--workload", "dml", "--size", "small", "--config", "gen1", "--yes")
    assert result.exit_code == 0, result.output
    assert sum(s.startswith("CREATE WAREHOUSE") for s in cursor.executed) == 1


def test_run_turns_a_cap_that_is_too_small_into_a_clean_error(runner, account):
    _cursor, conn, _fake = account()
    _stub_connection(conn)
    result = _invoke(runner, "run", "--max-clusters", "4", "--yes")
    assert result.exit_code == 1
    assert "Raise --max-credits" in result.output
    assert "Traceback" not in result.output


def test_run_rejects_a_throughput_multiplier_of_one(runner, account):
    _cursor, conn, _fake = account()
    _stub_connection(conn)
    result = _invoke(runner, "run", "--throughput-multiplier", "1", "--yes")
    assert result.exit_code == 1
    assert "0 (unlimited) or 2 or more" in result.output


def test_report_prints_the_crossovers_and_writes_the_csvs(runner, account, tmp_path, monkeypatch):
    rows = [cell_row("single", "XSMALL", config, wall_s=wall) for config, wall in (("gen1", 8.0), ("adaptive", 6.0))]
    _cursor, conn, _fake = account(cells=rows, metering={rows[0][1]: 1 / 60, rows[1][1]: 0.004})
    _stub_connection(conn)
    monkeypatch.chdir(tmp_path)

    result = _invoke(runner, "report")

    assert result.exit_code == 0, result.output
    assert "--- Step 2. Head to head" in result.output
    assert "single, Adaptive vs Gen1: faster at X-Small; cheaper as a fresh warehouse at X-Small" in result.output
    with (tmp_path / f"adaptive-compute-run-{RUN_ID}-crossovers.csv").open() as handle:
        (header, *data) = list(csv.reader(handle))
    assert header == report_core.COMPARISON_CSV_COLUMNS
    assert data[0][3:5] == ["adaptive", "gen1"]
    assert (tmp_path / f"adaptive-compute-run-{RUN_ID}.csv").exists()


def test_report_with_no_file_writes_nothing(runner, account, tmp_path, monkeypatch):
    _cursor, conn, _fake = account(cells=[cell_row("single", "XSMALL", "gen1")])
    _stub_connection(conn)
    monkeypatch.chdir(tmp_path)
    result = _invoke(runner, "report", "--no-file")
    assert result.exit_code == 0, result.output
    assert list(tmp_path.iterdir()) == []


def test_report_before_any_run_shows_up(runner, account):
    _cursor, conn, _fake = account(latest_run=None)
    _stub_connection(conn)
    result = _invoke(runner, "report")
    assert result.exit_code == 0
    assert "doesn't show a run" in result.output


def test_report_rejects_a_bad_run_id(runner, account):
    _cursor, conn, _fake = account()
    _stub_connection(conn)
    result = _invoke(runner, "report", "--run-id", "x")
    assert result.exit_code == 1 and "run id must look like" in result.output


def test_cleanup_confirms_then_drops(runner, account):
    _cursor, conn, fake = account()
    _stub_connection(conn)
    result = _invoke(runner, "cleanup", "--yes")
    assert result.exit_code == 0, result.output
    assert fake.warehouses == {} and fake.databases == {}


def test_cleanup_fails_loudly_when_something_is_left(runner, account):
    _cursor, conn, _fake = account(warehouses={"OLD_SETUP_WH": OURS})
    _stub_connection(conn)
    result = _invoke(runner, "cleanup", "--yes")
    assert result.exit_code == 1
    assert "left 1 object(s) behind" in result.output


def test_cleanup_everything(runner, account):
    _cursor, conn, fake = account(warehouses={"OLD_SETUP_WH": OURS})
    _stub_connection(conn)
    result = _invoke(runner, "cleanup", "--everything", "--yes")
    assert result.exit_code == 0, result.output
    assert fake.warehouses == {}


def test_a_default_run_leaves_the_monster_workloads_out(runner, account):
    cursor, conn, _fake = account()
    _stub_connection(conn)

    result = _invoke(runner, "run", input="n\n")

    assert "creates 45 warehouses" in result.output
    assert cursor.executed == []


def test_run_help_says_the_monster_workloads_are_opt_in(runner):
    result = runner.invoke(cli_module.adaptive_compute, ["run", "--help"])
    assert result.exit_code == 0
    assert "monster and monster-split are opt-in" in result.output
    assert "--adaptive-ceiling" in result.output and "--fanout" in result.output


def test_run_the_monster_workloads_with_a_ceiling(runner, account):
    cursor, conn, _fake = account()
    _stub_connection(conn)

    result = _invoke(
        runner, "run", "--workload", "monster", "--workload", "monster-split", "--size", "xsmall",
        "--config", "gen1", "--adaptive-ceiling", "xlarge", "--fanout", "4", "--yes",
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    creates = [s for s in cursor.executed if s.startswith("CREATE WAREHOUSE")]
    assert len(creates) == 4  # Gen1 X-Small and Adaptive X-Large, for each workload
    assert sum("MAX_QUERY_PERFORMANCE_LEVEL = XLARGE" in s for s in creates) == 2
    assert not any("MAX_CLUSTER_COUNT" in s for s in creates)
    assert sum(s.startswith("CREATE TEMPORARY TABLE") for s in cursor.executed) == 2 * 4


def test_max_clusters_given_sets_monster_split_too(runner, account):
    cursor, conn, _fake = account()
    _stub_connection(conn)

    result = _invoke(
        runner, "run", "--workload", "monster-split", "--workload", "concurrent", "--size", "xsmall",
        "--config", "gen2", "--max-clusters", "3", "--fanout", "2", "--yes",
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    creates = [s for s in cursor.executed if s.startswith("CREATE WAREHOUSE")]
    assert sum("MAX_CLUSTER_COUNT = 3" in s for s in creates) == 2


def test_run_rejects_a_fanout_out_of_range(runner, account):
    _cursor, conn, _fake = account()
    _stub_connection(conn)
    result = _invoke(runner, "run", "--workload", "monster", "--fanout", "1", "--yes")
    assert result.exit_code == 2
    assert "--fanout" in result.output
