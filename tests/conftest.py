"""Shared test fakes.

`FakeConnection` / `FakeCursor` stand in for a Snowflake connection: they record
every executed statement and return canned rows, so tests can assert on the SQL
a function issues without touching a real warehouse. They are handed to domain
functions through the same connection-injection seam the CLI uses.
"""

from __future__ import annotations

from typing import Any

import pytest


class FakeCursor:
    def __init__(
        self,
        *,
        fetch: list[tuple[Any, ...]] | None = None,
        description: list[tuple[Any, ...]] | None = None,
    ) -> None:
        self.executed: list[str] = []
        self._fetch = list(fetch) if fetch is not None else [("row",)]
        self.description = list(description) if description is not None else [("col",)]
        self.sfqid = "fake-query-id"
        self.closed = False

    def execute(self, sql: str, *args: Any) -> FakeCursor:
        self.executed.append(sql)
        return self

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._fetch)

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self._cursor

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def make_cursor():
    def _make(**kwargs: Any) -> FakeCursor:
        return FakeCursor(**kwargs)

    return _make


@pytest.fixture
def make_connection():
    def _make(cursor: FakeCursor) -> FakeConnection:
        return FakeConnection(cursor)

    return _make
