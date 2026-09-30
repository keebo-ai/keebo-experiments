"""A fake Snowflake account for the warehouse-sizing tests.

It answers the statements the benchmark issues by what they ask for — SHOW for
the ownership and table checks, the live-stats lookups, CURRENT_ROLE(), the
latest-run lookup — through the shared ``FakeCursor``'s ``route`` hook, so tests
read as "on an account where ...".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from experiments.warehouse_sizing_benchmark.core import infra, queries

GB = 1024**3
OURS = queries.OWNER_COMMENT
OBJECTS = infra.BenchmarkObjects.named()


@dataclass
class FakeAccount:
    """What exists on the account. ``None`` comment = the object doesn't exist."""

    warehouse_comment: str | None = OURS
    database_comment: str | None = OURS
    generation: str | None = "1"
    sample_data: bool = True  # can the role read SNOWFLAKE_SAMPLE_DATA?
    generated_table: bool = False  # has setup generated the fallback table?
    spill: tuple[Any, ...] = (5, 3 * GB, 0)  # (operators, bytes_local, bytes_remote) from operator stats
    elapsed_ms: Any = 42_000  # from QUERY_HISTORY_BY_SESSION; None = "not there yet"
    latest_run: str | None = "20260930-120000"

    def route(self, sql: str):
        if sql.startswith("SHOW WAREHOUSES"):
            rows = (
                [] if self.warehouse_comment is None else [(OBJECTS.warehouse, self.warehouse_comment, self.generation)]
            )
            return rows, [("name",), ("comment",), ("generation",)]
        if sql.startswith("SHOW DATABASES"):
            rows = [] if self.database_comment is None else [(OBJECTS.database, self.database_comment)]
            return rows, [("name",), ("comment",)]
        if sql.startswith("SHOW TABLES"):
            if "SNOWFLAKE_SAMPLE_DATA" in sql:
                if not self.sample_data:
                    # What Snowflake does for a missing or ungranted share: raise, not return nothing.
                    raise RuntimeError("Database 'SNOWFLAKE_SAMPLE_DATA' does not exist or not authorized.")
                return [("LINEITEM",)], [("name",)]
            if f"IN SCHEMA {OBJECTS.database}.PUBLIC" in sql:
                return ([("LINEITEM",)] if self.generated_table else []), [("name",)]
            return [("LINEITEM",)], [("name",)]  # any other explicit --table exists
        if "GET_QUERY_OPERATOR_STATS" in sql:
            return [self.spill], [("OPERATORS",), ("BYTES_LOCAL",), ("BYTES_REMOTE",)]
        if "QUERY_HISTORY_BY_SESSION" in sql:
            return [(self.elapsed_ms,)], [("ELAPSED_MS",)]
        if sql == "SELECT CURRENT_ROLE()":
            return [("SYSADMIN",)], [("CURRENT_ROLE()",)]
        if "MAX(SPLIT_PART(query_tag" in sql:
            return [(self.latest_run,)], [("RUN_ID",)]
        return None


@pytest.fixture
def account(make_cursor, make_connection):
    """``account(**settings) -> (cursor, connection)`` on a FakeAccount."""

    def _make(**settings: Any):
        fake = FakeAccount(**settings)
        cursor = make_cursor(route=fake.route)
        return cursor, make_connection(cursor)

    return _make
