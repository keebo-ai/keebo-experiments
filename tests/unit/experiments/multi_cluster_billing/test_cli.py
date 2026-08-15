"""CLI smoke tests: flags wire through to the domain layer, errors read cleanly."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from experiments.multi_cluster_billing import cli
from experiments.multi_cluster_billing.core import manifest, queries, verdict
from experiments.multi_cluster_billing.core import report as report_core
from experiments.multi_cluster_billing.core.questions import OWN_MINUTE, RULE_TEXT

RATE = 1.35


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@contextmanager
def fake_open(_connection_name: str | None = None) -> Iterator[Any]:
    """Stands in for `_open`: yields something a domain function would accept."""
    yield object()


def make_replicate(scenario: str = "control", index: int = 1) -> manifest.Replicate:
    return manifest.Replicate(
        scenario=scenario,
        index=index,
        warehouse=f"KEEBO_MCB_{scenario.upper()}_R{index}_20260813T140000",
        size="XSMALL",
        resource_constraint="STANDARD_GEN_2",
        target_clusters=1,
        cycle_seconds=90,
        kind="forced",
        resumed_at="2026-08-13T14:00:00+00:00",
        resume_confirmed_at="2026-08-13T14:00:03+00:00",
        scaled_at=None,
        target_seen_at=None,
        suspend_issued_at="2026-08-13T14:01:30+00:00",
        suspend_confirmed_at="2026-08-13T14:01:33+00:00",
        max_started_clusters=1,
        query_ids=[],
        polls=[],
        error=None,
    )


def make_run_manifest() -> manifest.RunManifest:
    return manifest.RunManifest(
        schema_version=manifest.SCHEMA_VERSION,
        run_token="20260813T140000",
        account="AB12345",
        region="AWS_US_EAST_1",
        snowflake_version="9.1.0",
        size="XSMALL",
        resource_constraint="STANDARD_GEN_2",
        started_at="2026-08-13T14:00:00+00:00",
        ended_at="2026-08-13T14:20:00+00:00",
        replicates=[make_replicate()],
    )


def observations(*, rule: str = OWN_MINUTE, replicates: int = 2, scenarios=None) -> list[verdict.Observation]:
    """Replicates of each scenario, billed exactly as `rule` says they would be.

    Built from the scenario table rather than hand-written numbers, so the CLI is
    exercised against the same shapes a real run produces.
    """
    built = []
    for spec in scenarios or queries.MEASURED_SCENARIOS:
        extras = [
            verdict.Extra(start_offset=float(spec.scale_out_at_seconds), seconds=float(spec.extra_cluster_seconds))
            for _ in range(spec.target_clusters - 1)
        ]
        warehouse_seconds = float(spec.cycle_seconds)
        for index in range(1, replicates + 1):
            built.append(
                verdict.Observation(
                    scenario=spec.name,
                    index=index,
                    warehouse=f"KEEBO_MCB_{spec.name.upper()}_R{index}_20260813T140000",
                    warehouse_seconds=warehouse_seconds,
                    extras=extras,
                    billed_seconds=verdict.predict(rule, warehouse_seconds=warehouse_seconds, extras=extras),
                )
            )
    return built


#: The scenarios that leave more than one rule standing: none of them has an extra
#: cluster that starts inside the first minute or outlives it, so several ways of
#: charging predict the same bill for all three.
UNDECIDING = [queries.SHORT, queries.CONTROL, queries.BRIEF]


def make_summary(
    *,
    inconclusive: bool = False,
    minimum_holds: bool = True,
    not_ready_reason: str | None = None,
) -> report_core.ReportSummary:
    seen = observations(scenarios=UNDECIDING if inconclusive else None)
    return report_core.ReportSummary(
        tables=[report_core.ReportTable(step=1, title="Run", columns=["run_token"], rows=[("20260813T140000",)])],
        meter_check=verdict.MeterCheck(
            published=RATE,
            size="XSMALL",
            resource_constraint="STANDARD_GEN_2",
            worst_gap_seconds=0.004,
            worst_warehouse=None,
            n=len(seen),
            ok=True,
        ),
        minimum_check=verdict.MinimumCheck(
            mean_billed=60.0 if minimum_holds else 45.0,
            mean_warehouse_seconds=45.0,
            n=2,
            holds=minimum_holds,
        ),
        verdict=None if not_ready_reason else verdict.compute_verdict(seen),
        not_ready_reason=not_ready_reason,
    )


def test_help_lists_the_three_commands(runner):
    result = runner.invoke(cli.cli, ["--help"])

    assert result.exit_code == 0
    for command in ("run", "report", "cleanup"):
        assert command in result.output


def test_help_states_the_questions_the_run_answers(runner):
    result = runner.invoke(cli.cli, ["--help"])

    assert "before it has run a minute" in result.output
    assert "when an extra cluster starts" in result.output


def test_run_defaults_to_four_replicates(monkeypatch, runner):
    captured = {}

    def fake_run_experiment(conn, **kwargs):
        captured.update(kwargs)
        return make_run_manifest()

    monkeypatch.setattr(cli.scenarios, "run_experiment", fake_run_experiment)
    monkeypatch.setattr(cli, "_open", fake_open)
    result = runner.invoke(cli.cli, ["run", "--yes"])
    assert result.exit_code == 0, result.output
    assert captured["replicates"] == 4
    assert captured["resource_constraint"] == "STANDARD_GEN_2"
    assert callable(captured["checkpoint"])


def test_the_batch_flag_is_gone(runner):
    result = runner.invoke(cli.cli, ["run", "--batch", "--yes"])
    assert result.exit_code != 0
    assert "no such option" in result.output.lower()


def test_the_checkpoint_writes_the_manifest(monkeypatch, tmp_path, runner):
    written = []

    def fake_run_experiment(conn, **kwargs):
        record = make_run_manifest()
        kwargs["checkpoint"](record)
        kwargs["checkpoint"](record)
        return record

    monkeypatch.setattr(cli.scenarios, "run_experiment", fake_run_experiment)
    monkeypatch.setattr(cli, "_open", fake_open)
    monkeypatch.setattr(cli.manifest, "save", lambda record, path: written.append(path) or path)
    result = runner.invoke(cli.cli, ["run", "--yes", "--manifest", str(tmp_path / "m.json")])
    assert result.exit_code == 0, result.output
    assert len(written) == 2
    assert all(str(p) == str(tmp_path / "m.json") for p in written)


def test_the_cost_confirmation_names_the_real_figures(runner, monkeypatch):
    monkeypatch.setattr(cli, "_open", fake_open)
    result = runner.invoke(cli.cli, ["run"], input="n\n")
    assert "about an hour" in result.output
    assert "2 credits" in result.output


def test_run_surfaces_a_domain_error_as_a_clean_message(monkeypatch, runner):
    def fake_run_experiment(conn, **kwargs):
        raise ValueError("requires the Enterprise edition")

    monkeypatch.setattr(cli.scenarios, "run_experiment", fake_run_experiment)
    monkeypatch.setattr(cli, "_open", fake_open)
    result = runner.invoke(cli.cli, ["run", "--yes"])

    assert result.exit_code != 0
    assert "Enterprise edition" in result.output


def test_report_shows_what_the_bills_were_decoded_with_before_the_verdict(monkeypatch, runner):
    monkeypatch.setattr(cli, "_open", fake_open)
    monkeypatch.setattr(cli.manifest, "load", lambda path: make_run_manifest())
    monkeypatch.setattr(cli.report_core, "read_report", lambda conn, run: make_summary())
    result = runner.invoke(cli.cli, ["report", "--manifest", "m.json"])
    assert result.exit_code == 0, result.output
    # The rate is what turns credits into seconds, so a reader has to be able to
    # check it before anything downstream of it is worth reading.
    assert result.output.index("credit rate") < result.output.index("Verdict")
    assert "published rate" in result.output and "1.3500 credits/hour" in result.output
    assert "whole seconds" in result.output


def test_report_names_every_rule_still_standing_when_inconclusive(monkeypatch, runner):
    monkeypatch.setattr(cli, "_open", fake_open)
    monkeypatch.setattr(cli.manifest, "load", lambda path: make_run_manifest())
    monkeypatch.setattr(cli.report_core, "read_report", lambda conn, run: make_summary(inconclusive=True))
    result = runner.invoke(cli.cli, ["report", "--manifest", "m.json"])
    assert "INCONCLUSIVE" in result.output
    # In words, not by constant name: the reader is told what each surviving rule
    # would mean, and which scenario would have told them apart.
    assert "cannot choose between" in result.output
    assert "charged a full minute at the least" in result.output


def test_report_leads_with_a_failed_minimum_premise(monkeypatch, runner):
    monkeypatch.setattr(cli, "_open", fake_open)
    monkeypatch.setattr(cli.manifest, "load", lambda path: make_run_manifest())
    monkeypatch.setattr(cli.report_core, "read_report", lambda conn, run: make_summary(minimum_holds=False))
    result = runner.invoke(cli.cli, ["report", "--manifest", "m.json"])
    assert "no 60-second minimum" in result.output.lower()


def test_report_prints_the_not_ready_reason_instead_of_a_verdict(monkeypatch, runner):
    monkeypatch.setattr(cli, "_open", fake_open)
    monkeypatch.setattr(cli.manifest, "load", lambda path: make_run_manifest())
    monkeypatch.setattr(
        cli.report_core,
        "read_report",
        lambda conn, run: make_summary(not_ready_reason="metering is not due until 18:00"),
    )
    result = runner.invoke(cli.cli, ["report", "--manifest", "m.json"])

    assert result.exit_code == 0, result.output
    assert "not due until 18:00" in result.output
    assert "Verdict" not in result.output


def test_cleanup_drops_the_manifest_s_warehouses(monkeypatch, tmp_path, runner):
    dropped = {}

    def fake_drop(conn, *, warehouses, echo):
        dropped["warehouses"] = warehouses

    path = manifest.save(make_run_manifest(), tmp_path / "run.json")
    monkeypatch.setattr(cli, "_open", fake_open)
    monkeypatch.setattr(cli.scenarios, "drop_warehouses", fake_drop)

    result = runner.invoke(cli.cli, ["cleanup", "--manifest", str(path), "--yes"])

    assert result.exit_code == 0, result.output
    assert dropped["warehouses"] == make_run_manifest().warehouses


def test_report_states_the_answer_in_words_before_the_scenario_by_scenario_detail(monkeypatch, runner):
    monkeypatch.setattr(cli, "_open", fake_open)
    monkeypatch.setattr(cli.manifest, "load", lambda path: make_run_manifest())
    monkeypatch.setattr(cli.report_core, "read_report", lambda conn, run: make_summary())
    with runner.isolated_filesystem():
        result = runner.invoke(cli.cli, ["report", "--manifest", "m.json"])

    assert result.exit_code == 0, result.output
    # The prose is wrapped to the terminal width, so it is read back unwrapped.
    assert f"The answer: {RULE_TEXT[OWN_MINUTE].says}." in " ".join(result.output.split())
    assert result.output.index("The answer:") < result.output.index("what each scenario ran")


def test_report_answers_each_question_with_its_evidence(monkeypatch, runner):
    monkeypatch.setattr(cli, "_open", fake_open)
    monkeypatch.setattr(cli.manifest, "load", lambda path: make_run_manifest())
    monkeypatch.setattr(cli.report_core, "read_report", lambda conn, run: make_summary())
    with runner.isolated_filesystem():
        result = runner.invoke(cli.cli, ["report", "--manifest", "m.json"])

    assert "the questions this run set out to answer" in result.output
    for number in range(1, 5):
        assert f"Q{number}." in result.output
    # Every question is stated, justified, backed by a measurement, and answered.
    for label in ("Why it matters:", "What we saw:", "Answer:"):
        assert label in result.output


def test_report_explains_each_scenario_and_what_its_numbers_settle(monkeypatch, runner):
    monkeypatch.setattr(cli, "_open", fake_open)
    monkeypatch.setattr(cli.manifest, "load", lambda path: make_run_manifest())
    monkeypatch.setattr(cli.report_core, "read_report", lambda conn, run: make_summary())
    with runner.isolated_filesystem():
        result = runner.invoke(cli.cli, ["report", "--manifest", "m.json"])

    for spec in queries.MEASURED_SCENARIOS:
        assert f"  {spec.name}\n" in result.output
    for label in ("What it ran:", "Why:", "Numbers:", "Conclusion:"):
        assert label in result.output


def test_report_writes_the_results_and_verdict_to_a_file(monkeypatch, runner):
    monkeypatch.setattr(cli, "_open", fake_open)
    monkeypatch.setattr(cli.manifest, "load", lambda path: make_run_manifest())
    monkeypatch.setattr(cli.report_core, "read_report", lambda conn, run: make_summary())
    with runner.isolated_filesystem():
        result = runner.invoke(cli.cli, ["report", "--manifest", "m.json"])
        written = Path("cluster-billing-run-20260813T140000.txt")

        assert result.exit_code == 0, result.output
        assert written.exists(), result.output
        text = written.read_text()

    # The file is the whole report, not just the headline: the run it came from,
    # the tables, the questions, the answer in words, and the numbers behind it.
    assert "20260813T140000" in text and "AB12345" in text
    assert "Step 1. Run" in text
    assert "The answer:" in text
    assert OWN_MINUTE in text
    assert "the questions this run set out to answer" in text
    assert str(written) in result.output


def test_the_out_flag_chooses_where_the_report_lands(monkeypatch, runner):
    monkeypatch.setattr(cli, "_open", fake_open)
    monkeypatch.setattr(cli.manifest, "load", lambda path: make_run_manifest())
    monkeypatch.setattr(cli.report_core, "read_report", lambda conn, run: make_summary())
    with runner.isolated_filesystem():
        result = runner.invoke(cli.cli, ["report", "--manifest", "m.json", "--out", "out/answer.txt"])

        assert result.exit_code == 0, result.output
        assert "The answer:" in Path("out/answer.txt").read_text()


def test_a_report_with_no_verdict_yet_is_still_written(monkeypatch, runner):
    monkeypatch.setattr(cli, "_open", fake_open)
    monkeypatch.setattr(cli.manifest, "load", lambda path: make_run_manifest())
    monkeypatch.setattr(
        cli.report_core,
        "read_report",
        lambda conn, run: make_summary(not_ready_reason="metering is not due until 18:00"),
    )
    with runner.isolated_filesystem():
        result = runner.invoke(cli.cli, ["report", "--manifest", "m.json"])

        assert result.exit_code == 0, result.output
        assert "not due until 18:00" in Path("cluster-billing-run-20260813T140000.txt").read_text()


def test_the_no_file_flag_prints_without_writing(monkeypatch, runner):
    monkeypatch.setattr(cli, "_open", fake_open)
    monkeypatch.setattr(cli.manifest, "load", lambda path: make_run_manifest())
    monkeypatch.setattr(cli.report_core, "read_report", lambda conn, run: make_summary())
    with runner.isolated_filesystem():
        result = runner.invoke(cli.cli, ["report", "--manifest", "m.json", "--no-file"])

        assert result.exit_code == 0, result.output
        assert list(Path().glob("*.txt")) == []
    assert "The answer:" in result.output
