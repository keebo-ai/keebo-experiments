"""A fake Snowflake account for the spillage tests.

It answers the statements the demo issues by what they ask for — SHOW for the
ownership checks, the live-stats lookup, CURRENT_ROLE() — through the shared
``FakeCursor``'s ``route`` hook, so tests read as "on an account where ...".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from experiments.spillage.core import infra, queries

GB = 1024**3
OURS = queries.OWNER_COMMENT
OBJECTS = infra.DemoObjects.named()


@dataclass
class FakeAccount:
    """What exists on the account. ``None`` comment = the object doesn't exist."""

    warehouse_comment: str | None = OURS
    database_comment: str | None = OURS
    generation: str | None = "1"
    # One (bytes_local, bytes_remote, elapsed_ms) row per lookup, or [] for "not there yet".
    live_stats: list[tuple[Any, ...]] = field(default_factory=lambda: [(3 * GB, 0, 42_000)])
    # Optional per-lookup answers (e.g. [[], [], [row]] = "arrives on the third try"),
    # used up in order before falling back to ``live_stats``.
    live_stats_sequence: list[list[tuple[Any, ...]]] = field(default_factory=list)

    def route(self, sql: str):
        if sql.startswith("SHOW WAREHOUSES"):
            rows = (
                [] if self.warehouse_comment is None else [(OBJECTS.warehouse, self.warehouse_comment, self.generation)]
            )
            return rows, [("name",), ("comment",), ("generation",)]
        if sql.startswith("SHOW DATABASES"):
            rows = [] if self.database_comment is None else [(OBJECTS.database, self.database_comment)]
            return rows, [("name",), ("comment",)]
        if "QUERY_HISTORY_BY_SESSION" in sql:
            rows = self.live_stats_sequence.pop(0) if self.live_stats_sequence else self.live_stats
            return rows, [("BYTES_LOCAL",), ("BYTES_REMOTE",), ("ELAPSED_MS",)]
        if sql == "SELECT CURRENT_ROLE()":
            return [("SYSADMIN",)], [("CURRENT_ROLE()",)]
        return None


@pytest.fixture
def account(make_cursor, make_connection):
    """``account(**settings) -> (cursor, connection)`` on a FakeAccount."""

    def _make(**settings: Any):
        fake = FakeAccount(**settings)
        cursor = make_cursor(route=fake.route)
        return cursor, make_connection(cursor)

    return _make
