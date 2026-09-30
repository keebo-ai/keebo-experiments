"""Unit tests for the shared SQL helpers."""

from __future__ import annotations

import pytest

from common import sql


def test_validate_identifier_accepts_plain_name():
    assert sql.validate_identifier("MY_WH", "warehouse") == "MY_WH"


def test_validate_identifier_accepts_qualified_name():
    assert sql.validate_identifier("DB.SCHEMA.TABLE$1", "table") == "DB.SCHEMA.TABLE$1"


@pytest.mark.parametrize("bad", ["bad; DROP TABLE x", "with space", "quote'name", ""])
def test_validate_identifier_rejects_unsafe(bad):
    with pytest.raises(ValueError, match="warehouse must match"):
        sql.validate_identifier(bad, "warehouse")


def test_validate_name_upper_cases_like_snowflake():
    assert sql.validate_name("my_demo_wh", "warehouse") == "MY_DEMO_WH"


@pytest.mark.parametrize("bad", ["DB.WH", "1WH", "wh-dash", "quote'name", "bad; DROP", ""])
def test_validate_name_rejects_anything_but_one_plain_name(bad):
    with pytest.raises(ValueError, match="warehouse must be a single unquoted name"):
        sql.validate_name(bad, "warehouse")
