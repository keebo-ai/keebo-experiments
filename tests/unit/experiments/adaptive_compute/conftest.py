"""A fake Snowflake account for the adaptive-compute tests.

It answers the statements the experiment issues by what they ask for, through
the shared ``FakeCursor``'s ``route`` hook, and keeps track of which warehouses
exist: a CREATE WAREHOUSE adds one, a DROP removes it. So ``claim`` and
``drop`` see what a real account would, and tests read as "on an account where ...".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import pytest

from experiments.adaptive_compute.core import infra, queries

OURS = queries.OWNER_COMMENT
OBJECTS = infra.HomeObjects.named()
RUN_ID = "20261014_143318"

CELL_COLUMNS = [
    "query_tag",
    "warehouse_name",
    "statements",
    "failed",
    "distinct_hashes",
    "wall_s",
    "elapsed_s",
    "queued_s",
    "clusters",
    "busy_s",
    "floor_s",
]


class SnowflakeTimeout(Exception):
    """What the connector raises when a statement hits its timeout (error 000630)."""

    errno = 630


def cell_row(workload: str, size: str, config: str, replicate: int = 1, **values: Any) -> tuple[Any, ...]:
    """One CELLS_SQL row, with plausible defaults for anything not given."""
    code = queries.CONFIGS_BY_KEY[config].code
    wcode = queries.WORKLOADS_BY_KEY[workload].code
    defaults = {
        "statements": 1,
        "failed": 0,
        "distinct_hashes": 1,
        "wall_s": 10.0,
        "elapsed_s": 10.0,
        "queued_s": 0.0,
        "clusters": 1,
        "busy_s": 10.0,
        "floor_s": 60.0,
    }
    defaults.update(values)
    return (
        f"adcomp:{RUN_ID}:{workload}:{size}:{config}:{replicate}",
        f"{OBJECTS.warehouse}_{RUN_ID}_{code}_{size}_{wcode}_{replicate}",
        *(defaults[c] for c in CELL_COLUMNS[2:]),
    )


@dataclass
class FakeAccount:
    """What exists on the account. Warehouses and databases map name -> comment."""

    warehouses: dict[str, str] = field(default_factory=lambda: {OBJECTS.warehouse: OURS})
    databases: dict[str, str] = field(default_factory=lambda: {OBJECTS.database: OURS})
    sample_data: bool = True
    base_table: bool = True
    latest_run: str | None = RUN_ID
    cells: list[tuple[Any, ...]] = field(default_factory=list)
    metering: dict[str, float] = field(default_factory=dict)  # warehouse name -> billed credits
    query_metering: dict[str, float] = field(default_factory=dict)  # query tag -> credits
    # Statements to fail: a SQL fragment -> what to do with each matching statement, in order:
    # an exception to raise, or None to let that one through.
    fail: dict[str, list[Exception | None]] = field(default_factory=dict)

    def route(self, sql: str):
        for fragment, outcomes in self.fail.items():
            if fragment in sql and outcomes and (error := outcomes.pop(0)) is not None:
                raise error
        if sql.startswith("SHOW WAREHOUSES") or sql.startswith("SHOW DATABASES"):
            objects = self.warehouses if "WAREHOUSES" in sql else self.databases
            like = re.search(r"LIKE '([^']*)'", sql)
            pattern = like.group(1) if like else "%"
            matches = [
                (name, comment)
                for name, comment in objects.items()
                if (name.startswith(pattern[:-1]) if pattern.endswith("%") else name == pattern)
            ]
            return matches, [("name",), ("comment",)]
        if sql.startswith("CREATE WAREHOUSE") or sql.startswith("CREATE TRANSIENT DATABASE"):
            is_warehouse = sql.startswith("CREATE WAREHOUSE")
            name = sql.split()[2 if is_warehouse else 3]
            comment = re.search(r"COMMENT = '([^']*)'", sql).group(1)
            (self.warehouses if is_warehouse else self.databases)[name] = comment
            return None
        if sql.startswith("DROP WAREHOUSE") or sql.startswith("DROP DATABASE"):
            name = sql.split()[-1]
            (self.warehouses if sql.startswith("DROP WAREHOUSE") else self.databases).pop(name, None)
            return None
        if sql.startswith("SHOW TABLES"):
            if "SNOWFLAKE_SAMPLE_DATA" in sql:
                if not self.sample_data:
                    raise RuntimeError("Database 'SNOWFLAKE_SAMPLE_DATA' does not exist or not authorized.")
                return [("LINEITEM",)], [("name",)]
            name = re.search(r"LIKE '([^']*)'", sql).group(1)
            return ([(name,)] if self.base_table else []), [("name",)]
        if sql == "SELECT CURRENT_ROLE()":
            return [("SYSADMIN",)], [("CURRENT_ROLE()",)]
        if "MAX(SPLIT_PART(query_tag" in sql:
            return [(self.latest_run,)], [("RUN_ID",)]
        if "WITH q AS" in sql:
            return list(self.cells), [(c.upper(),) for c in CELL_COLUMNS]
        if "WAREHOUSE_METERING_HISTORY" in sql and "GROUP BY warehouse_name" in sql:
            return list(self.metering.items()), [("WAREHOUSE_NAME",), ("BILLED_CREDITS",)]
        if "QUERY_METERING_HISTORY" in sql and "GROUP BY query_tag" in sql:
            return list(self.query_metering.items()), [("QUERY_TAG",), ("QUERY_CREDITS",)]
        return None


class FakeClock:
    """A clock that moves ``step`` seconds every time it's read, and as far as asked when slept."""

    def __init__(self, step: float = 1.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def account(make_cursor, make_connection):
    """``account(**settings) -> (cursor, connection, fake)`` on a FakeAccount."""

    def _make(**settings: Any):
        fake = FakeAccount(**settings)
        cursor = make_cursor(route=fake.route)
        return cursor, make_connection(cursor), fake

    return _make


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def snowflake_timeout():
    """``snowflake_timeout()`` -> the exception the connector raises when a statement hits its timeout."""
    return lambda: SnowflakeTimeout("Statement reached its statement or warehouse timeout")
