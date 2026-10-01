"""Unit tests for the shared warehouse size and rate table."""

from __future__ import annotations

import pytest

from common import warehouses


def test_sizes_run_xsmall_to_xxlarge_doubling():
    assert warehouses.SIZE_KEYWORDS == ("XSMALL", "SMALL", "MEDIUM", "LARGE", "XLARGE", "XXLARGE")
    assert warehouses.CREDITS_PER_HOUR["XXLARGE"] == 32
    rates = [credits for _, _, credits in warehouses.SIZES]
    assert rates == [1, 2, 4, 8, 16, 32]
    assert warehouses.SIZE_LABEL["XXLARGE"] == "2X-Large"


def test_multiplier_is_gen2s_premium():
    assert warehouses.multiplier("1") == 1.0
    assert warehouses.multiplier("2") == pytest.approx(1.35)
    with pytest.raises(ValueError, match="generation"):
        warehouses.multiplier("3")


def test_credits_per_hour_applies_the_generation():
    assert warehouses.credits_per_hour("MEDIUM") == 4
    assert warehouses.credits_per_hour("medium", "2") == pytest.approx(4 * 1.35)


@pytest.mark.parametrize(("size", "generation", "match"), [("HUGE", "1", "size"), ("XSMALL", "3", "generation")])
def test_credits_per_hour_rejects_unknown_values(size, generation, match):
    with pytest.raises(ValueError, match=match):
        warehouses.credits_per_hour(size, generation)


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"generation": "2"}, "2"),
        ({"resource_constraint": "STANDARD_GEN_1"}, "1"),
        ({"resource_constraint": "MEMORY_16X"}, None),  # Snowpark-optimized: no generation
        ({}, None),
    ],
)
def test_generation_of_reads_either_column(row, expected):
    assert warehouses.generation_of(row) == expected


def test_generation_of_prefers_resource_constraint_like_multi_cluster_billing():
    assert warehouses.generation_of({"resource_constraint": "STANDARD_GEN_2", "generation": "1"}) == "2"
