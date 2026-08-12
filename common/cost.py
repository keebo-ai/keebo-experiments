"""Pre-run credit estimates for experiments that spend warehouse compute.

Pure math — no connection, no ``click`` — so an experiment can print what a run
will cost before spending anything. Actual credits always come from metering
after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass

# Snowflake bills a 60-second minimum each time a warehouse (or cluster) starts;
# a run shorter than this still costs a full minute of the configured size.
BILLING_MINIMUM_SECONDS = 60

# Credits per hour per running cluster, by warehouse size (Snowflake Standard
# rates). Each size step doubles the previous one.
CREDITS_PER_HOUR_BY_SIZE: dict[str, int] = {
    "XSMALL": 1,
    "SMALL": 2,
    "MEDIUM": 4,
    "LARGE": 8,
    "XLARGE": 16,
    "2XLARGE": 32,
    "3XLARGE": 64,
    "4XLARGE": 128,
    "5XLARGE": 256,
    "6XLARGE": 512,
}

_SECONDS_PER_HOUR = 3600


@dataclass(frozen=True)
class CostEstimate:
    size: str
    clusters: int
    requested_seconds: float
    billed_seconds: float
    credits: float


def estimate(size: str, clusters: int, seconds: float) -> CostEstimate:
    """Estimate credits for ``clusters`` running ``size`` for ``seconds``.

    Applies the 60-second billing minimum so a short run doesn't look
    deceptively cheap.
    """
    normalized = size.upper()
    if normalized not in CREDITS_PER_HOUR_BY_SIZE:
        raise ValueError(f"Unknown warehouse size: {size!r}")
    if clusters < 1:
        raise ValueError(f"clusters must be >= 1, got {clusters}")

    billed_seconds = max(seconds, BILLING_MINIMUM_SECONDS)
    credits = CREDITS_PER_HOUR_BY_SIZE[normalized] * clusters * billed_seconds / _SECONDS_PER_HOUR
    return CostEstimate(
        size=normalized,
        clusters=clusters,
        requested_seconds=seconds,
        billed_seconds=billed_seconds,
        credits=credits,
    )
