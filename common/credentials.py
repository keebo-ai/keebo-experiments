"""Shared credential resolution and connection opening for the CLI.

The ``click``-aware layer on top of :mod:`common.snowflake` (which stays free of
``click``). Every experiment command uses these so credential handling — env,
Snowflake's connection config, and interactive prompts — behaves identically
everywhere.

Resolution order matches CONTRIBUTING.md:

1. ``--connection NAME``: that entry in Snowflake's config.
2. ``SNOWFLAKE_*`` env vars (from ``.env``), when ``SNOWFLAKE_ACCOUNT`` is set:
   a deliberate choice for this repo beats the machine-wide default.
3. Snowflake's default connection, if one is set up (as the Snowflake CLI uses).
4. Otherwise, prompts for whatever the environment doesn't supply.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import click
from dotenv import load_dotenv

from common import snowflake as sf

# Load .env once, so credentials can live in a git-ignored file.
load_dotenv()

# Shared across every experiment command, so their contracts never drift.
connection_option = click.option(
    "--connection",
    "connection_name",
    default=None,
    help=(
        "Name of a connection in Snowflake's config (config.toml / connections.toml). "
        "If omitted: SNOWFLAKE_* env / .env when SNOWFLAKE_ACCOUNT is set, else "
        "your Snowflake default connection, else prompts."
    ),
)


def resolve_credentials() -> sf.SnowflakeCredentials:
    """Build credentials from the environment, prompting for what's missing."""
    env = sf.env_credentials()
    account = env["account"] or click.prompt("Snowflake account")
    user = env["user"] or click.prompt("Snowflake user")
    password = env["password"]
    authenticator = env["authenticator"]
    # Need one credential; prompt for a password only if SSO isn't configured.
    if not password and not authenticator:
        password = click.prompt("Snowflake password", hide_input=True)
    return sf.SnowflakeCredentials(
        account=account,
        user=user,
        password=password,
        role=env["role"],
        authenticator=authenticator,
    )


@contextmanager
def open_connection(connection_name: str | None) -> Iterator[Any]:
    """Open a Snowflake connection as a context manager, resolving it in the order above."""
    name = connection_name or _implicit_connection_name()
    if name:
        with sf.connection(connection_name=name) as conn:
            yield conn
    else:
        with sf.connection(creds=resolve_credentials()) as conn:
            yield conn


def _implicit_connection_name() -> str | None:
    """The default connection to use when no ``--connection`` was given, if any.

    ``SNOWFLAKE_ACCOUNT`` in the environment means the user set up ``.env`` for
    this repo, which wins over the machine-wide default.
    """
    if sf.env_credentials()["account"]:
        return None
    name = sf.default_connection_name()
    if name:
        click.echo(
            f"Using your Snowflake default connection '{name}' (pass --connection NAME to choose another).", err=True
        )
    return name
