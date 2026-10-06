"""Shared credential resolution (env, prompts, SSO)."""

from __future__ import annotations

from contextlib import contextmanager

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


# --------------------------------------------------------------------------- #
# Which connection open_connection picks
# --------------------------------------------------------------------------- #
class _Opened:
    """Records what ``sf.connection`` was asked to open."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)

        @contextmanager
        def _cm():
            yield "conn"

        return _cm()


def _setup(monkeypatch, *, env_account=None, default=None, known=()):
    for var in ("ACCOUNT", "USER", "PASSWORD", "ROLE", "AUTHENTICATOR"):
        monkeypatch.delenv(f"SNOWFLAKE_{var}", raising=False)
    if env_account:
        monkeypatch.setenv("SNOWFLAKE_ACCOUNT", env_account)
        monkeypatch.setenv("SNOWFLAKE_USER", "user")
        monkeypatch.setenv("SNOWFLAKE_PASSWORD", "pw")
    monkeypatch.setattr(
        credentials.sf, "_connector_config", lambda: (default or "default", {name: {} for name in known})
    )
    opened = _Opened()
    monkeypatch.setattr(credentials.sf, "connection", opened)
    return opened


def test_explicit_connection_wins(monkeypatch):
    opened = _setup(monkeypatch, env_account="acct", known=["default", "mine"])
    with credentials.open_connection("mine"):
        pass
    assert opened.calls == [{"connection_name": "mine"}]


def test_env_account_beats_the_default_connection(monkeypatch):
    opened = _setup(monkeypatch, env_account="acct", known=["default"])
    with credentials.open_connection(None):
        pass
    [call] = opened.calls
    assert call["creds"].account == "acct"


def test_default_connection_is_used_when_nothing_else_is_set(monkeypatch, capsys):
    opened = _setup(monkeypatch, known=["default"])
    with credentials.open_connection(None):
        pass
    assert opened.calls == [{"connection_name": "default"}]
    assert "Using your Snowflake default connection 'default'" in capsys.readouterr().err


def test_a_configured_default_name_is_honoured(monkeypatch):
    opened = _setup(monkeypatch, default="work", known=["default", "work"])
    with credentials.open_connection(None):
        pass
    assert opened.calls == [{"connection_name": "work"}]


def test_prompts_when_there_is_no_default_connection(monkeypatch):
    opened = _setup(monkeypatch, known=[])
    answers = iter(["acct", "user", "pw"])
    monkeypatch.setattr(credentials.click, "prompt", lambda *a, **k: next(answers))
    with credentials.open_connection(None):
        pass
    [call] = opened.calls
    assert call["creds"] == SnowflakeCredentials(account="acct", user="user", password="pw")
