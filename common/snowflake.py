"""Shared Snowflake connection client for Keebo experiments.

Credentials can come from three places; the CLI layer decides the precedence
(named connection > environment > interactive prompt) and this module provides
the pieces:

1. **A named connection** in Snowflake's own ``connections.toml`` (the file the
   Snowflake CLI uses, at ``~/.snowflake/connections.toml`` or
   ``~/.config/snowflake/connections.toml``). See :func:`connect_named`.
2. **Environment variables** ``SNOWFLAKE_ACCOUNT`` / ``SNOWFLAKE_USER`` /
   ``SNOWFLAKE_PASSWORD`` / ``SNOWFLAKE_ROLE`` / ``SNOWFLAKE_AUTHENTICATOR``
   (loaded from ``.env`` by the CLI). See :func:`env_credentials`.
3. **Interactive prompts** for anything still missing — that lives in the CLI,
   since prompting is a UI concern and this module stays free of it.

Whichever the source, :func:`connection` hands experiment code an open
connection and closes it on exit. Experiment code depends only on that open
connection — never on how it was created — which is the injection seam.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any


def _get_optional(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


@dataclass(frozen=True)
class SnowflakeCredentials:
    """Explicit connection settings. Immutable so it can be passed around freely.

    Provide either a ``password`` or an ``authenticator`` (e.g.
    ``"externalbrowser"`` for SSO).
    """

    account: str
    user: str
    password: str | None = None
    role: str | None = None
    authenticator: str | None = None


def env_credentials() -> dict[str, str | None]:
    """Return the raw ``SNOWFLAKE_*`` values from the environment.

    Any value may be ``None``; the CLI fills the gaps (prompting) and decides
    what's required. Kept side-effect-free so it's trivial to test.
    """
    return {
        "account": _get_optional("SNOWFLAKE_ACCOUNT"),
        "user": _get_optional("SNOWFLAKE_USER"),
        "password": _get_optional("SNOWFLAKE_PASSWORD"),
        "role": _get_optional("SNOWFLAKE_ROLE"),
        "authenticator": _get_optional("SNOWFLAKE_AUTHENTICATOR"),
    }


def _connector() -> Any:
    """Import the Snowflake connector lazily (heavy optional dependency)."""
    from snowflake import connector  # noqa: PLC0415

    return connector


def connect(creds: SnowflakeCredentials) -> Any:
    """Open a connection from explicit credentials.

    Only the optional settings that were provided are passed, so the connector
    applies its own defaults for the rest.
    """
    kwargs: dict[str, str] = {"account": creds.account, "user": creds.user}
    for field_name in ("password", "role", "authenticator"):
        value = getattr(creds, field_name)
        if value is not None:
            kwargs[field_name] = value
    return _connector().connect(**kwargs)


def connect_named(connection_name: str) -> Any:
    """Open a connection from a named entry in Snowflake's ``connections.toml``."""
    return _connector().connect(connection_name=connection_name)


@contextmanager
def connection(
    *,
    creds: SnowflakeCredentials | None = None,
    connection_name: str | None = None,
) -> Iterator[Any]:
    """Yield an open connection and close it on exit.

    Pass exactly one of ``creds`` (explicit settings) or ``connection_name`` (a
    ``connections.toml`` entry).
    """
    if (creds is None) == (connection_name is None):
        raise ValueError("Pass exactly one of `creds` or `connection_name`.")

    conn = connect_named(connection_name) if connection_name else connect(creds)
    try:
        yield conn
    finally:
        conn.close()
