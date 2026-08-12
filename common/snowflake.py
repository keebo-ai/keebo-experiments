"""Shared Snowflake connection client for Keebo experiments.

Required environment variables (loaded from ``.env`` by each experiment's CLI):

* ``SNOWFLAKE_ACCOUNT``        account identifier, e.g. ``myorg-myaccount``
* ``SNOWFLAKE_USER``           username

Plus at least one credential:

* ``SNOWFLAKE_PASSWORD``       password, or
* ``SNOWFLAKE_AUTHENTICATOR``  auth method, e.g. ``externalbrowser`` for SSO

Optional:

* ``SNOWFLAKE_ROLE``           role to connect with (needs ACCOUNT_USAGE access
                               for the reporting queries)

The design keeps this module free of any CLI concern: read credentials with
:func:`credentials_from_env`, then open a connection with :func:`connection` (a
context manager that closes it on exit). Experiment code depends only on the
open connection it is handed — never on how it was created — which is the
injection seam experiments use and tests exploit.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any


def _get_or_fail(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is not set")
    return value.strip()


def _get_optional(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


@dataclass(frozen=True)
class SnowflakeCredentials:
    """Everything needed to open a connection. Immutable so it can be passed
    around freely without any caller mutating it."""

    account: str
    user: str
    password: str | None = None
    role: str | None = None
    authenticator: str | None = None


def credentials_from_env() -> SnowflakeCredentials:
    """Build credentials from the ``SNOWFLAKE_*`` environment variables.

    Raises ``ValueError`` if the account/user or a usable credential is missing;
    the CLI layer turns that into a clean ``click.ClickException``.
    """
    account = _get_or_fail("SNOWFLAKE_ACCOUNT")
    user = _get_or_fail("SNOWFLAKE_USER")
    password = _get_optional("SNOWFLAKE_PASSWORD")
    role = _get_optional("SNOWFLAKE_ROLE")
    authenticator = _get_optional("SNOWFLAKE_AUTHENTICATOR")

    if not password and not authenticator:
        raise ValueError(
            "No Snowflake credential found. Set SNOWFLAKE_PASSWORD, or SNOWFLAKE_AUTHENTICATOR=externalbrowser for SSO."
        )

    return SnowflakeCredentials(
        account=account,
        user=user,
        password=password,
        role=role,
        authenticator=authenticator,
    )


def connect(creds: SnowflakeCredentials) -> Any:
    """Open a Snowflake connection from ``creds``.

    The connector is imported lazily so that code which only *injects* a
    connection (or merely reads ``--help``) never needs it installed.
    """
    from snowflake import connector  # noqa: PLC0415  (lazy: heavy optional dep)

    # Only pass the optional settings that were provided, so the connector
    # applies its own defaults for the rest.
    kwargs: dict[str, str] = {"account": creds.account, "user": creds.user}
    for field_name in ("password", "role", "authenticator"):
        value = getattr(creds, field_name)
        if value is not None:
            kwargs[field_name] = value

    return connector.connect(**kwargs)


@contextmanager
def connection(creds: SnowflakeCredentials) -> Iterator[Any]:
    """Yield an open connection built from ``creds`` and close it on exit."""
    conn = connect(creds)
    try:
        yield conn
    finally:
        conn.close()
