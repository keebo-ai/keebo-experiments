"""Unit tests for the benchmark SQL/constants."""

from __future__ import annotations

import pytest

from experiments.warehouse_sizing_benchmark.core import queries


def test_sizes_and_query_are_consistent():
    assert [row[0] for row in queries.SIZES] == queries.SIZE_KEYWORDS
    assert len(queries.SIZES) == 6
    assert "IDENTIFIER($lineitem_table)" in queries.BENCHMARK_QUERY


def test_report_steps_cover_10_through_16():
    assert [step for step, _, _ in queries.REPORT_STEPS] == [10, 11, 12, 13, 14, 15, 16]


def test_validate_identifier():
    assert queries.validate_identifier("SCHEMA.TABLE$1", "table") == "SCHEMA.TABLE$1"
    with pytest.raises(ValueError, match="warehouse"):
        queries.validate_identifier("bad; DROP", "warehouse")
