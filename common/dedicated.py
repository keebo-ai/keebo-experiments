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

from typing import Any

from common.sql import validate_name

KINDS = ("WAREHOUSE", "DATABASE")


def owner_comment(experiment: str) -> str:
    """The COMMENT that marks an object as created by ``experiment``."""
    return f"Created by keebo-experiments {experiment} - safe to drop"


def find(cur: Any, kind: str, name: str) -> dict[str, Any] | None:
    """The ``SHOW <kind>S`` row for ``name`` (columns keyed by lower-case name), or ``None``."""
    _check_kind(kind)
    name = validate_name(name, kind.lower())
    cur.execute(f"SHOW {kind}S LIKE '{name}'")
    columns = [column[0].lower() for column in cur.description]
    # LIKE treats "_" as a wildcard, so match the name exactly.
    for row in cur.fetchall():
        record = dict(zip(columns, row, strict=False))
        if str(record.get("name", "")).upper() == name:
            return record
    return None


def claim(cur: Any, kind: str, name: str, *, comment: str) -> dict[str, Any] | None:
    """Return the object's row if this experiment owns it, ``None`` if it doesn't exist.

    Raises ``ValueError`` if an object with that name exists but wasn't created
    by this experiment.
    """
    name = validate_name(name, kind.lower())
    record = find(cur, kind, name)
    if record is not None and record.get("comment") != comment:
        noun = kind.lower()
        raise ValueError(
            f"{noun} {name} already exists and wasn't created by this experiment, so it won't be "
            f"touched. Choose a different name with --{noun}."
        )
    return record


def drop(cur: Any, kind: str, name: str, *, comment: str) -> bool:
    """Drop the object if this experiment owns it. Returns whether anything was dropped."""
    name = validate_name(name, kind.lower())
    if claim(cur, kind, name, comment=comment) is None:
        return False
    cur.execute(f"DROP {kind} IF EXISTS {name}")
    return True


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}, got {kind!r}")
