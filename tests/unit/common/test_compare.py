"""Unit tests for the shared comparison wording."""

from __future__ import annotations

import pytest

from common import compare


@pytest.mark.parametrize(
    ("before", "after", "text"),
    [(76.0, 10.0, "7.6x faster"), (10.0, 13.0, "1.3x slower"), (10.0, 10.0, "1.0x faster"), (None, 3.0, None)],
)
def test_speed_change(before, after, text):
    assert compare.speed_change(before, after) == text


@pytest.mark.parametrize(
    ("before", "after", "text"),
    [
        (0.02, 0.06, "3.0x as much"),
        (0.02, 0.03, "50% more"),
        (0.02, 0.01, "50% cheaper"),
        (0.02, 0.02001, "about the same"),
        (0, 0.01, None),
    ],
)
def test_cost_change(before, after, text):
    assert compare.cost_change(before, after) == text
