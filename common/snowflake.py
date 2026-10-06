"""Shared Snowflake connection client for Keebo experiments.

Credentials can come from four places; the CLI layer decides the precedence
(named connection > environment > default connection > interactive prompt) and
this module provides the pieces:

1. **A named connection** from Snowflake's own config — the ``[connections]``
   in ``config.toml`` or ``connections.toml``, the files the Snowflake CLI uses,
   under ``~/.snowflake/`` (or ``SNOWFLAKE_HOME``). See :func:`connect_named`.
2. **Environment variables** ``SNOWFLAKE_ACCOUNT`` / ``SNOWFLAKE_USER`` /
   ``SNOWFLAKE_PASSWORD`` / ``SNOWFLAKE_ROLE`` / ``SNOWFLAKE_AUTHENTICATOR``
   (loaded from ``.env`` by the CLI). See :func:`env_credentials`.
3. **The default connection** in that same config, if one is set up. See
   :func:`default_connection_name`.
4. **Interactive prompts** for anything still missing — that lives in the CLI,
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


def _connector_config() -> tuple[str, dict[str, Any]]:
    """The connector's default connection name and its known connections."""
    from snowflake.connector.config_manager import CONFIG_MANAGER  # noqa: PLC0415

    return CONFIG_MANAGER["default_connection_name"], CONFIG_MANAGER["connections"]


def default_connection_name() -> str | None:
    """The name of Snowflake's default connection, or ``None`` if none is set up.

    Resolved exactly as the connector resolves it: the ``default_connection_name``
    setting, falling back to a connection called ``default``. A config that
    can't be read counts as none, so the caller falls back to prompting.
    """
    try:
        name, connections = _connector_config()
    except Exception:  # an unreadable config means "no default connection"
        return None
    return name if name in connections else None


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
    """Open a connection from a named entry in Snowflake's config (``config.toml`` / ``connections.toml``)."""
    return _connector().connect(connection_name=connection_name)


@contextmanager
def connection(
    *,
    creds: SnowflakeCredentials | None = None,
    connection_name: str | None = None,
) -> Iterator[Any]:
    """Yield an open connection and close it on exit.

    Pass exactly one of ``creds`` (explicit settings) or ``connection_name`` (an
    entry in Snowflake's config).
    """
    if (creds is None) == (connection_name is None):
        raise ValueError("Pass exactly one of `creds` or `connection_name`.")

    conn = connect_named(connection_name) if connection_name else connect(creds)
    try:
        yield conn
    finally:
        conn.close()
