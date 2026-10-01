"""Snowflake warehouse sizes and what they cost, shared by every experiment.

Pure data and arithmetic, with no database or CLI dependencies. These are the
published standard-warehouse rates for Generation 1; Generation 2 bills the
same sizes at a fixed multiple.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Each entry: (ALTER WAREHOUSE keyword, name recorded in QUERY_HISTORY, Gen1 credits/hr).
SIZES: tuple[tuple[str, str, int], ...] = (
    ("XSMALL", "X-Small", 1),
    ("SMALL", "Small", 2),
    ("MEDIUM", "Medium", 4),
    ("LARGE", "Large", 8),
    ("XLARGE", "X-Large", 16),
    ("XXLARGE", "2X-Large", 32),
)
SIZE_KEYWORDS = tuple(keyword for keyword, _, _ in SIZES)
SIZE_LABEL = {keyword: label for keyword, label, _ in SIZES}
CREDITS_PER_HOUR = {keyword: credits for keyword, _, credits in SIZES}

GENERATIONS = ("1", "2")
GEN2_MULTIPLIER = 1.35

# Snowflake bills at least this much each time a warehouse resumes.
BILLING_MINIMUM_SECONDS = 60


def multiplier(generation: str) -> float:
    """How much more than Gen1 a warehouse of ``generation`` ('1' or '2') costs per hour."""
    if generation not in GENERATIONS:
        raise ValueError(f"generation must be one of {', '.join(GENERATIONS)}, got {generation!r}")
    return GEN2_MULTIPLIER if generation == "2" else 1.0


def credits_per_hour(size: str, generation: str = "1") -> float:
    """The published hourly rate for ``size`` on warehouse ``generation`` ('1' or '2')."""
    if size.upper() not in CREDITS_PER_HOUR:
        raise ValueError(f"size must be one of {', '.join(SIZE_KEYWORDS)}, got {size!r}")
    return CREDITS_PER_HOUR[size.upper()] * multiplier(generation)


def generation_of(show_row: Mapping[str, Any]) -> str | None:
    """The generation ('1' / '2') a ``SHOW WAREHOUSES`` row reports, or ``None`` if it doesn't.

    Accounts report it under one of two column names, matching the two DDL
    properties: ``resource_constraint`` ('STANDARD_GEN_1') or ``generation`` ('1').
    """
    value = show_row.get("resource_constraint") or show_row.get("generation")
    if value is None:
        return None
    digit = str(value).strip()[-1:]
    return digit if digit in GENERATIONS else None
