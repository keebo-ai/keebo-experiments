"""Snowflake warehouse sizes and what they cost, shared by every experiment.

Pure data and arithmetic — no database or CLI dependencies. These are the
published standard-warehouse rates for Generation 1; Generation 2 bills the
same sizes at a fixed multiple.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Each entry: (ALTER WAREHOUSE keyword, name recorded in QUERY_HISTORY, Gen1 credits/hr).
SIZES: list[tuple[str, str, int]] = [
    ("XSMALL", "X-Small", 1),
    ("SMALL", "Small", 2),
    ("MEDIUM", "Medium", 4),
    ("LARGE", "Large", 8),
    ("XLARGE", "X-Large", 16),
    ("XXLARGE", "2X-Large", 32),
]
SIZE_KEYWORDS = [keyword for keyword, _, _ in SIZES]
SIZE_LABEL = {keyword: label for keyword, label, _ in SIZES}

GENERATIONS = ("1", "2")
GEN2_MULTIPLIER = 1.35

# Snowflake bills at least this much each time a warehouse resumes.
BILLING_MINIMUM_SECONDS = 60


def credits_per_hour(size: str, generation: str = "1") -> float:
    """The published hourly rate for ``size`` on warehouse ``generation`` ('1' or '2')."""
    rates = {keyword: credits for keyword, _, credits in SIZES}
    if size.upper() not in rates:
        raise ValueError(f"size must be one of {', '.join(SIZE_KEYWORDS)}, got {size!r}")
    if generation not in GENERATIONS:
        raise ValueError(f"generation must be one of {', '.join(GENERATIONS)}, got {generation!r}")
    multiplier = GEN2_MULTIPLIER if generation == "2" else 1.0
    return rates[size.upper()] * multiplier


def generation_of(show_row: Mapping[str, Any]) -> str | None:
    """The generation ('1' / '2') a ``SHOW WAREHOUSES`` row reports, or ``None`` if it doesn't.

    Accounts report it under one of two column names, matching the two DDL
    properties: ``generation`` ('1') or ``resource_constraint`` ('STANDARD_GEN_1').
    """
    value = show_row.get("generation") or show_row.get("resource_constraint")
    if value is None:
        return None
    digit = str(value).strip()[-1:]
    return digit if digit in GENERATIONS else None
