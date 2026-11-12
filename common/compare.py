"""Say how two measurements compare, in words a report can print.

Pure arithmetic and wording, with no database or CLI dependencies. Each function
returns ``None`` when the comparison can't be made (a missing or zero value), so
the caller decides how to show a gap.
"""

from __future__ import annotations


def speed_change(before_s: float | None, after_s: float | None) -> str | None:
    """How much faster ``after_s`` is than ``before_s``: ``'7.6x faster'`` or ``'1.3x slower'``."""
    if not before_s or not after_s:
        return None
    ratio = before_s / after_s
    return f"{ratio:.1f}x faster" if ratio >= 1 else f"{1 / ratio:.1f}x slower"


def cost_change(before: float | None, after: float | None) -> str | None:
    """What ``after`` costs next to ``before``: ``'3.0x as much'``, ``'50% more'``, or ``'50% cheaper'``.

    A big increase reads as a ratio and a small change as a percentage.
    """
    if not before or not after:
        return None
    ratio = after / before
    if ratio >= 2:
        return f"{ratio:.1f}x as much"
    diff = (ratio - 1) * 100
    if round(diff) == 0:
        return "about the same"
    return f"{abs(diff):.0f}% cheaper" if diff < 0 else f"{diff:.0f}% more"
