"""The run manifest: what `run` observed, so `report` can read it back later.

The two commands are separate because metering is bucketed hourly and lags up to
three hours. Lifetimes are read back from WAREHOUSE_EVENTS_HISTORY, which lags
just as long, so the manifest's job is to record what was run and against which
warehouses. The polled observations it also keeps are a cross-check on those
events, not the measurement.

Timestamps are ISO-8601 with a UTC offset, as strings, so the file is readable
and the round-trip is lossless.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from experiments.multi_cluster_billing.core import queries

# 3 replaced the per-scenario `reps` list with a flat `replicates` list: one
# warehouse per cycle, so one metering row per cycle. v1 ran three cycles inside
# one warehouse, which summed into a single row and made replicate-to-replicate
# variation — and therefore any confidence interval — impossible to see.
# `load` rejects older versions rather than reading them: a manifest is only
# useful alongside the run it describes.
SCHEMA_VERSION = 3
FILENAME_PREFIX = "cluster-billing-run-"


@dataclass(frozen=True)
class Poll:
    """One `SHOW WAREHOUSES` observation."""

    at: str
    state: str
    started_clusters: int
    queued: int


@dataclass
class Replicate:
    """One warehouse, driven through one resume / scale-out / suspend cycle.

    The polled timestamps are kept as a cross-check, not as the measurement: the
    verdict reads durations from WAREHOUSE_EVENTS_HISTORY, and `report` warns
    when the two disagree by more than a few seconds, which indicates a missed
    event rather than a result.
    """

    scenario: str
    index: int
    warehouse: str
    size: str
    resource_constraint: str
    target_clusters: int
    cycle_seconds: int
    kind: str
    resumed_at: str
    resume_confirmed_at: str
    scaled_at: str | None
    target_seen_at: str | None
    suspend_issued_at: str
    suspend_confirmed_at: str
    max_started_clusters: int
    query_ids: list[str]
    polls: list[Poll]
    error: str | None


@dataclass
class RunManifest:
    schema_version: int
    run_token: str
    account: str
    region: str
    snowflake_version: str
    size: str
    resource_constraint: str
    started_at: str
    # None until the run finishes: the file is written after every warehouse so a
    # crash 20 minutes in still leaves `cleanup` something to work from.
    ended_at: str | None
    replicates: list[Replicate]

    @property
    def warehouses(self) -> list[str]:
        """Every warehouse this run created, in run order."""
        return [r.warehouse for r in self.replicates]

    def for_scenario(self, name: str) -> list[Replicate]:
        """Every replicate of ``name``, in run order."""
        return [r for r in self.replicates if r.scenario == name]


def run_token(now: datetime) -> str:
    """A compact UTC stamp, safe to embed in a warehouse identifier."""
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%S")


def default_path(token: str, directory: Path | str = ".") -> Path:
    return Path(directory) / f"{FILENAME_PREFIX}{token}.json"


def report_path(token: str, directory: Path | str = ".") -> Path:
    """Where the readable report for ``token`` is written.

    Same stem as the manifest, so a run's input and its answer sort together and
    the one ignore rule already covers both: this file names the account's
    warehouses too.
    """
    return Path(directory) / f"{FILENAME_PREFIX}{token}.txt"


def save(run: RunManifest, path: Path | str) -> Path:
    """Write ``run`` as pretty JSON and return the path written."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(asdict(run), indent=2, sort_keys=False) + "\n")
    return target


def load(path: Path | str) -> RunManifest:
    """Read a manifest written by :func:`save`."""
    raw = json.loads(Path(path).read_text())
    version = raw.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(f"manifest schema version {version} is not supported (expected {SCHEMA_VERSION})")
    return RunManifest(
        schema_version=raw["schema_version"],
        run_token=raw["run_token"],
        account=raw["account"],
        region=raw["region"],
        snowflake_version=raw["snowflake_version"],
        size=raw["size"],
        resource_constraint=raw["resource_constraint"],
        started_at=raw["started_at"],
        ended_at=raw["ended_at"],
        replicates=[
            Replicate(
                scenario=item["scenario"],
                index=item["index"],
                warehouse=item["warehouse"],
                size=item["size"],
                resource_constraint=item["resource_constraint"],
                target_clusters=item["target_clusters"],
                cycle_seconds=item["cycle_seconds"],
                kind=item["kind"],
                resumed_at=item["resumed_at"],
                resume_confirmed_at=item["resume_confirmed_at"],
                scaled_at=item["scaled_at"],
                target_seen_at=item["target_seen_at"],
                suspend_issued_at=item["suspend_issued_at"],
                suspend_confirmed_at=item["suspend_confirmed_at"],
                max_started_clusters=item["max_started_clusters"],
                query_ids=list(item["query_ids"]),
                polls=[Poll(**poll) for poll in item["polls"]],
                error=item["error"],
            )
            for item in raw["replicates"]
        ],
    )


def latest_path(directory: Path | str = ".") -> Path | None:
    """The most recent manifest in ``directory``, or ``None`` if there is none.

    Filenames carry a sortable UTC stamp, so lexical order is chronological.
    """
    candidates = sorted(Path(directory).glob(f"{FILENAME_PREFIX}*.json"))
    return candidates[-1] if candidates else None


def metering_ready_by(run: RunManifest) -> datetime:
    """When the metering rows should exist at the latest.

    A metering row only appears once its hour has closed, and the view then lags
    up to three hours on top of that. Three hours is Snowflake's documented
    worst case, not a schedule: rows often land well before this. So this is an
    upper bound for explaining an empty result, never a gate on querying —
    whether the data is there is answered by asking for it.
    """
    ended = datetime.fromisoformat(run.ended_at or run.started_at).astimezone(UTC)
    hour_close = ended.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return hour_close + timedelta(hours=queries.METERING_LAG_HOURS)
