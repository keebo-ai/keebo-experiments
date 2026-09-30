"""Unit tests for finding, claiming, and dropping experiment-owned objects."""

from __future__ import annotations

import pytest

from common import dedicated

OURS = dedicated.owner_comment("demo")
SHOW_COLUMNS = [("name",), ("comment",)]


def _cursor(make_cursor, rows):
    return make_cursor(fetch=rows, description=SHOW_COLUMNS)


def test_owner_comment_names_the_experiment():
    assert "keebo-experiments demo" in OURS
    assert "'" not in OURS  # it's interpolated into COMMENT = '...'


def test_find_matches_the_exact_name_despite_like_wildcards(make_cursor):
    # LIKE 'MY_WH' also matches MYXWH, so only the exact name counts.
    cursor = _cursor(make_cursor, [("MYXWH", OURS), ("MY_WH", "other")])

    record = dedicated.find(cursor, "WAREHOUSE", "MY_WH")

    assert cursor.executed == ["SHOW WAREHOUSES LIKE 'MY_WH'"]
    assert record == {"name": "MY_WH", "comment": "other"}


def test_find_returns_none_when_absent(make_cursor):
    assert dedicated.find(_cursor(make_cursor, []), "DATABASE", "DB") is None


def test_claim_returns_our_object(make_cursor):
    record = dedicated.claim(_cursor(make_cursor, [("MY_WH", OURS)]), "WAREHOUSE", "MY_WH", comment=OURS)
    assert record["name"] == "MY_WH"


def test_claim_refuses_someone_elses_object(make_cursor):
    cursor = _cursor(make_cursor, [("COMPUTE_WH", "production")])

    with pytest.raises(ValueError, match="wasn't created by this experiment.*--warehouse"):
        dedicated.claim(cursor, "WAREHOUSE", "COMPUTE_WH", comment=OURS)


def test_drop_only_drops_our_object(make_cursor):
    cursor = _cursor(make_cursor, [("DB", OURS)])
    assert dedicated.drop(cursor, "DATABASE", "DB", comment=OURS) is True
    assert cursor.executed[-1] == "DROP DATABASE IF EXISTS DB"


def test_drop_is_a_no_op_when_absent(make_cursor):
    cursor = _cursor(make_cursor, [])
    assert dedicated.drop(cursor, "DATABASE", "DB", comment=OURS) is False
    assert not any(s.startswith("DROP") for s in cursor.executed)


def test_drop_refuses_someone_elses_object(make_cursor):
    cursor = _cursor(make_cursor, [("DB", "production")])
    with pytest.raises(ValueError, match="--database"):
        dedicated.drop(cursor, "DATABASE", "DB", comment=OURS)
    assert not any(s.startswith("DROP") for s in cursor.executed)


def test_unknown_kind_is_rejected(make_cursor):
    with pytest.raises(ValueError, match="kind"):
        dedicated.find(make_cursor(), "TABLE", "T")
