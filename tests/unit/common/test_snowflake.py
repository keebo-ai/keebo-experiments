"""Unit tests for the shared Snowflake client."""

from __future__ import annotations

import pytest

from common import snowflake as sf


def test_env_credentials_reads_all_vars(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "myorg-acct")
    monkeypatch.setenv("SNOWFLAKE_USER", "me")
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "secret")
    monkeypatch.setenv("SNOWFLAKE_ROLE", "ACCOUNTADMIN")
    monkeypatch.delenv("SNOWFLAKE_AUTHENTICATOR", raising=False)

    assert sf.env_credentials() == {
        "account": "myorg-acct",
        "user": "me",
        "password": "secret",
        "role": "ACCOUNTADMIN",
        "authenticator": None,
    }


def test_env_credentials_missing_values_are_none(monkeypatch):
    for var in ("ACCOUNT", "USER", "PASSWORD", "ROLE", "AUTHENTICATOR"):
        monkeypatch.delenv(f"SNOWFLAKE_{var}", raising=False)

    assert sf.env_credentials() == {
        "account": None,
        "user": None,
        "password": None,
        "role": None,
        "authenticator": None,
    }


def test_connect_passes_only_provided_settings(monkeypatch):
    from snowflake import connector

    captured: dict[str, str] = {}
    monkeypatch.setattr(connector, "connect", lambda **kwargs: captured.update(kwargs) or object())

    sf.connect(sf.SnowflakeCredentials(account="a", user="u", authenticator="externalbrowser"))

    assert captured == {"account": "a", "user": "u", "authenticator": "externalbrowser"}
    assert "password" not in captured  # None fields are omitted
    assert "role" not in captured


def test_connect_named_passes_connection_name(monkeypatch):
    from snowflake import connector

    captured: dict[str, str] = {}
    monkeypatch.setattr(connector, "connect", lambda **kwargs: captured.update(kwargs) or object())

    sf.connect_named("mydemo")

    assert captured == {"connection_name": "mydemo"}


def test_connection_requires_exactly_one_source():
    with pytest.raises(ValueError, match="exactly one"):
        with sf.connection():
            pass


def test_connection_closes_on_exit(monkeypatch):
    from snowflake import connector

    class _Conn:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    conn = _Conn()
    monkeypatch.setattr(connector, "connect", lambda **kwargs: conn)

    with sf.connection(creds=sf.SnowflakeCredentials(account="a", user="u", password="p")) as opened:
        assert opened is conn
        assert not conn.closed

    assert conn.closed
