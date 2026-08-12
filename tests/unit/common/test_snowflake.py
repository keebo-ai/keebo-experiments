"""Unit tests for the shared Snowflake client."""

from __future__ import annotations

import pytest

from common import snowflake as sf


def test_credentials_from_env_reads_all_vars(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "myorg-acct")
    monkeypatch.setenv("SNOWFLAKE_USER", "me")
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "secret")
    monkeypatch.setenv("SNOWFLAKE_ROLE", "ACCOUNTADMIN")
    monkeypatch.delenv("SNOWFLAKE_AUTHENTICATOR", raising=False)

    creds = sf.credentials_from_env()

    assert creds == sf.SnowflakeCredentials(account="myorg-acct", user="me", password="secret", role="ACCOUNTADMIN")


def test_credentials_from_env_missing_account_raises(monkeypatch):
    monkeypatch.delenv("SNOWFLAKE_ACCOUNT", raising=False)
    monkeypatch.setenv("SNOWFLAKE_USER", "me")
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "secret")

    with pytest.raises(ValueError, match="SNOWFLAKE_ACCOUNT is not set"):
        sf.credentials_from_env()


def test_credentials_from_env_requires_a_credential(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "myorg-acct")
    monkeypatch.setenv("SNOWFLAKE_USER", "me")
    monkeypatch.delenv("SNOWFLAKE_PASSWORD", raising=False)
    monkeypatch.delenv("SNOWFLAKE_AUTHENTICATOR", raising=False)

    with pytest.raises(ValueError, match="No Snowflake credential"):
        sf.credentials_from_env()


def test_connect_passes_only_provided_settings(monkeypatch):
    from snowflake import connector

    captured: dict[str, str] = {}
    monkeypatch.setattr(connector, "connect", lambda **kwargs: captured.update(kwargs) or object())

    sf.connect(sf.SnowflakeCredentials(account="a", user="u", authenticator="externalbrowser"))

    assert captured == {"account": "a", "user": "u", "authenticator": "externalbrowser"}
    assert "password" not in captured  # None fields are omitted
    assert "role" not in captured


def test_connection_closes_on_exit(monkeypatch):
    from snowflake import connector

    class _Conn:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    conn = _Conn()
    monkeypatch.setattr(connector, "connect", lambda **kwargs: conn)

    with sf.connection(sf.SnowflakeCredentials(account="a", user="u", password="p")) as opened:
        assert opened is conn
        assert not conn.closed

    assert conn.closed
