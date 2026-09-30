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
