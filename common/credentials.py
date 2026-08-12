"""Shared credential resolution and connection opening for the CLI.

The ``click``-aware layer on top of :mod:`common.snowflake` (which stays free of
``click``). Every experiment command uses these so credential handling — env,
``connections.toml``, and interactive prompts — behaves identically everywhere.

Resolution order matches CONTRIBUTING.md: a ``--connection NAME`` entry in
Snowflake's ``connections.toml`` wins; otherwise ``SNOWFLAKE_*`` env vars (from
``.env``) fill in, and anything still missing is prompted for.
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
        "Name of an entry in Snowflake's connections.toml to connect with. "
        "If omitted, credentials come from SNOWFLAKE_* env / .env, prompting "
        "for anything missing."
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
    """Open a Snowflake connection as a context manager.

    Uses the named ``connections.toml`` entry if given; otherwise resolves
    credentials from the environment, prompting for anything missing.
    """
    if connection_name:
        with sf.connection(connection_name=connection_name) as conn:
            yield conn
    else:
        with sf.connection(creds=resolve_credentials()) as conn:
            yield conn
