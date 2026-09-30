"""Shared test fakes.

`FakeConnection` / `FakeCursor` stand in for a Snowflake connection: they record
every executed statement and return canned rows, so tests can assert on the SQL
a function issues without touching a real warehouse. They are handed to domain
functions through the same connection-injection seam the CLI uses.

Two extras exist for the multi-cluster billing experiment, which polls the same
statement repeatedly and submits queries asynchronously:

- ``responses`` queues one ``(rows, description)`` pair per ``execute`` call, so
  a test can say "the third poll shows two clusters".
- ``execute_async`` / ``get_query_status`` / ``is_still_running`` mirror the
  connector's async API, with per-query-id queues of "still running?" answers.
"""

from __future__ import annotations

from typing import Any

import pytest

Rows = list[tuple[Any, ...]]
Description = list[tuple[Any, ...]]


class FakeCursor:
    def __init__(
        self,
        *,
        fetch: Rows | None = None,
        description: Description | None = None,
        responses: list[tuple[Rows, Description]] | None = None,
        connection: FakeConnection | None = None,
    ) -> None:
        self.executed: list[str] = []
        self._fetch = list(fetch) if fetch is not None else [("row",)]
        self._default_description = list(description) if description is not None else [("col",)]
        self.description = list(self._default_description)
        self._responses = list(responses) if responses else []
        self._current: Rows = list(self._fetch)
        self.sfqid = "fake-query-id"
        self.closed = False
        self.connection = connection

    def _advance(self) -> None:
        if self._responses:
            rows, description = self._responses.pop(0)
            self._current = list(rows)
            self.description = list(description)
        else:
            self._current = list(self._fetch)
            self.description = list(self._default_description)

    def execute(self, sql: str, *args: Any) -> FakeCursor:
        self.executed.append(sql)
        self._advance()
        return self

    def execute_async(self, sql: str, *args: Any) -> FakeCursor:
        self.executed.append(sql)
        self._advance()
        if self.connection is not None:
            self.sfqid = self.connection.next_query_id()
        return self

    def fetchall(self) -> Rows:
        return list(self._current)

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        cursor.connection = self
        self.closed = False
        self.query_states: dict[str, list[bool]] = {}
        self._query_counter = 0

    def cursor(self) -> FakeCursor:
        return self._cursor

    def next_query_id(self) -> str:
        self._query_counter += 1
        return f"fake-query-{self._query_counter}"

    def get_query_status(self, query_id: str) -> str:
        queue = self.query_states.get(query_id)
        running = queue.pop(0) if queue else False
        return "RUNNING" if running else "SUCCESS"

    def is_still_running(self, status: str) -> bool:
        return status == "RUNNING"

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
