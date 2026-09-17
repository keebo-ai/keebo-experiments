"""Credit estimate math."""

from __future__ import annotations

import pytest

from common.cost import BILLING_MINIMUM_SECONDS, estimate


def test_applies_billing_minimum():
    result = estimate("XSMALL", clusters=1, seconds=20)

    assert result.billed_seconds == BILLING_MINIMUM_SECONDS
    assert result.credits == pytest.approx(1 * 1 * 60 / 3600)


def test_scales_with_size_and_clusters():
    result = estimate("MEDIUM", clusters=3, seconds=120)

    assert result.credits == pytest.approx(4 * 3 * 120 / 3600)


def test_rejects_unknown_size():
    with pytest.raises(ValueError, match="Unknown warehouse size"):
        estimate("TITANIC", clusters=1, seconds=60)
