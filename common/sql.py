"""SQL helpers shared across Keebo experiments.

Identifiers reach experiment code from CLI flags, so anything interpolated into
a statement is validated here first — belt-and-suspenders against injection,
since Snowflake has no bind parameter for an identifier position.
"""

from __future__ import annotations

import re

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.$]+$")
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def validate_identifier(value: str, label: str) -> str:
    """Return ``value`` if it is a safe SQL identifier, else raise ``ValueError``."""
    if not _IDENTIFIER_RE.match(value):
        raise ValueError(f"{label} must match [A-Za-z0-9_.$]+, got {value!r}")
    return value


def validate_name(value: str, label: str) -> str:
    """Return ``value`` upper-cased if it is a safe one-part object name, else raise ``ValueError``.

    For the name of a warehouse or database an experiment creates. Snowflake
    stores an unquoted name in upper case, and that is how ``SHOW`` and
    ``ACCOUNT_USAGE`` report it, so upper-casing here keeps string comparisons
    against them correct (``--warehouse my_wh`` is ``MY_WH`` everywhere).
    """
    if not _NAME_RE.match(value):
        raise ValueError(f"{label} must be a single unquoted name like MY_{label.upper()}, got {value!r}")
    return value.upper()
