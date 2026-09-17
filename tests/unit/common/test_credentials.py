"""Shared credential resolution (env, prompts, SSO)."""

from __future__ import annotations

from common import credentials
from common.snowflake import SnowflakeCredentials


def test_resolve_credentials_from_env(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "acct")
    monkeypatch.setenv("SNOWFLAKE_USER", "user")
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "pw")
    monkeypatch.setenv("SNOWFLAKE_ROLE", "SYSADMIN")
    monkeypatch.delenv("SNOWFLAKE_AUTHENTICATOR", raising=False)

    creds = credentials.resolve_credentials()

    assert creds == SnowflakeCredentials(account="acct", user="user", password="pw", role="SYSADMIN")


def test_resolve_credentials_prompts_for_missing(monkeypatch):
    for var in ("ACCOUNT", "USER", "PASSWORD", "ROLE", "AUTHENTICATOR"):
        monkeypatch.delenv(f"SNOWFLAKE_{var}", raising=False)
    answers = iter(["acct", "user", "pw"])  # account, user, password prompts
    monkeypatch.setattr(credentials.click, "prompt", lambda *a, **k: next(answers))

    creds = credentials.resolve_credentials()

    assert creds == SnowflakeCredentials(account="acct", user="user", password="pw", role=None)


def test_resolve_credentials_skips_password_prompt_with_sso(monkeypatch):
    for var in ("PASSWORD", "ROLE"):
        monkeypatch.delenv(f"SNOWFLAKE_{var}", raising=False)
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "acct")
    monkeypatch.setenv("SNOWFLAKE_USER", "user")
    monkeypatch.setenv("SNOWFLAKE_AUTHENTICATOR", "externalbrowser")

    def _no_prompt(*args, **kwargs):
        raise AssertionError("should not prompt when SSO is configured")

    monkeypatch.setattr(credentials.click, "prompt", _no_prompt)

    creds = credentials.resolve_credentials()

    assert creds == SnowflakeCredentials(account="acct", user="user", password=None, authenticator="externalbrowser")
