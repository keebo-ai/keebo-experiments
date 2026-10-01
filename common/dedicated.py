"""Find, claim, and drop the Snowflake objects an experiment creates for itself.

Experiments run on other people's accounts, so every warehouse or database one
creates carries an owner comment, and nothing is resized, suspended, or dropped
unless that comment is present. A same-named object without it belongs to
someone else, and touching it raises ``ValueError`` instead.

Functions take an open cursor and a name, which is checked with
:func:`common.sql.validate_name` (upper-cased, one part) before it goes into
any SQL. No ``click`` here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from common.snowflake import rows
from common.sql import validate_name

KINDS = ("WAREHOUSE", "DATABASE")


def find(cur: Any, kind: str, name: str) -> dict[str, Any] | None:
    """The ``SHOW <kind>S`` row for ``name`` (columns keyed by lower-case name), or ``None``."""
    return _find(cur, kind, validate_name(name, kind.lower()))


def claim(cur: Any, kind: str, name: str, *, comment: str) -> dict[str, Any] | None:
    """Return the object's row if this experiment owns it, ``None`` if it doesn't exist.

    Raises ``ValueError`` if an object with that name exists but wasn't created
    by this experiment.
    """
    name = validate_name(name, kind.lower())
    record = _find(cur, kind, name)
    if record is not None and record.get("comment") != comment:
        raise ValueError(
            f"{kind.lower()} {name} already exists and this experiment didn't create it, so it won't be "
            "touched. Pick a different name."
        )
    return record


def drop(cur: Any, kind: str, name: str, *, comment: str) -> bool:
    """Drop the object if this experiment owns it. Returns whether anything was dropped."""
    name = validate_name(name, kind.lower())
    if claim(cur, kind, name, comment=comment) is None:
        return False
    cur.execute(f"DROP {kind} IF EXISTS {name}")
    return True


def suspend_quietly(cur: Any, warehouse: str, echo: Callable[[str], None] | None = None) -> None:
    """Suspend ``warehouse``, ignoring errors. The caller must already have claimed it.

    For ``finally`` blocks, where a failure (e.g. "already suspended") would
    otherwise hide the real exception. A warehouse's AUTO_SUSPEND is the backstop.
    """
    try:
        cur.execute(f"ALTER WAREHOUSE {validate_name(warehouse, 'warehouse')} SUSPEND")
    except Exception as exc:  # best effort by design
        if echo:
            echo(f"  (couldn't suspend {warehouse}: {exc}; it auto-suspends when idle)")


def _find(cur: Any, kind: str, name: str) -> dict[str, Any] | None:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}, got {kind!r}")
    cur.execute(f"SHOW {kind}S LIKE '{name}'")
    # LIKE treats "_" as a wildcard, so match the name exactly.
    return next((row for row in rows(cur) if str(row.get("name", "")).upper() == name), None)
