"""SQL helpers shared across Keebo experiments.

Identifiers reach experiment code from CLI flags, so anything interpolated into
a statement is validated here first — belt-and-suspenders against injection,
since Snowflake has no bind parameter for an identifier position.
"""

from __future__ import annotations

import re

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.$]+$")


def validate_identifier(value: str, label: str) -> str:
    """Return ``value`` if it is a safe SQL identifier, else raise ``ValueError``."""
    if not _IDENTIFIER_RE.match(value):
        raise ValueError(f"{label} must match [A-Za-z0-9_.$]+, got {value!r}")
    return value
