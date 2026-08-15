"""Unit tests for the run manifest."""

from __future__ import annotations

import json

import pytest

from experiments.multi_cluster_billing.core import manifest


def replicate(scenario="k2", index=1, warehouse="KEEBO_MCB_K2_R1_T") -> manifest.Replicate:
    return manifest.Replicate(
        scenario=scenario,
        index=index,
        warehouse=warehouse,
        size="XSMALL",
        resource_constraint="STANDARD_GEN_2",
        target_clusters=2,
        cycle_seconds=90,
        kind="forced",
        resumed_at="2026-08-14T05:00:00+00:00",
        resume_confirmed_at="2026-08-14T05:00:03+00:00",
        scaled_at="2026-08-14T05:01:10+00:00",
        target_seen_at="2026-08-14T05:01:15+00:00",
        suspend_issued_at="2026-08-14T05:01:30+00:00",
        suspend_confirmed_at="2026-08-14T05:01:33+00:00",
        max_started_clusters=2,
        query_ids=[],
        polls=[manifest.Poll(at="2026-08-14T05:00:01+00:00", state="STARTED", started_clusters=1, queued=0)],
        error=None,
    )


def run(replicates=None) -> manifest.RunManifest:
    return manifest.RunManifest(
        schema_version=manifest.SCHEMA_VERSION,
        run_token="20260814T050000",
        account="ACC",
        region="AWS_US_WEST_2",
        snowflake_version="9.0.0",
        size="XSMALL",
        resource_constraint="STANDARD_GEN_2",
        started_at="2026-08-14T05:00:00+00:00",
        ended_at="2026-08-14T05:30:00+00:00",
        replicates=list(replicates if replicates is not None else [replicate()]),
    )


def test_round_trip_is_lossless(tmp_path):
    original = run()
    path = manifest.save(original, tmp_path / "m.json")
    assert manifest.load(path) == original


def test_a_manifest_can_be_written_before_the_run_ends(tmp_path):
    # `run` checkpoints after every warehouse, so cleanup has something to work
    # from if it crashes 20 minutes in.
    partial = run()
    partial.ended_at = None  # RunManifest is mutable: the run loop stamps this at the end
    path = manifest.save(partial, tmp_path / "m.json")
    assert manifest.load(path).ended_at is None


def test_load_rejects_an_older_schema(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"schema_version": 2}))
    with pytest.raises(ValueError, match="schema version 2"):
        manifest.load(path)


def test_warehouses_lists_every_replicate_in_run_order():
    record = run([replicate("short", 1, "W_SHORT_R1"), replicate("k5", 2, "W_K5_R2")])
    assert record.warehouses == ["W_SHORT_R1", "W_K5_R2"]


def test_for_scenario_selects_that_scenarios_replicates():
    record = run([replicate("k2", 1, "A"), replicate("control", 1, "B"), replicate("k2", 2, "C")])
    assert [r.warehouse for r in record.for_scenario("k2")] == ["A", "C"]
    assert record.for_scenario("nope") == []


def test_metering_ready_by_clears_the_hour_then_adds_the_lag():
    from experiments.multi_cluster_billing.core import queries

    ready = manifest.metering_ready_by(run())
    assert ready.hour == (6 + queries.METERING_LAG_HOURS) % 24
    assert ready.minute == 0


def test_latest_path_picks_the_newest(tmp_path):
    (tmp_path / f"{manifest.FILENAME_PREFIX}20260814T050000.json").write_text("{}")
    (tmp_path / f"{manifest.FILENAME_PREFIX}20260814T060000.json").write_text("{}")
    assert manifest.latest_path(tmp_path).name.endswith("20260814T060000.json")
