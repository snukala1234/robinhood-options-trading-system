"""Environment gating: secrets from env only, localhost only, live fail-closed."""

from __future__ import annotations

import pytest

from src.config import environments
from src.config.environments import ConfigurationError, OperatingMode


def test_missing_database_url_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ConfigurationError, match="DATABASE_URL is not set"):
        environments.database_url()


def test_remote_database_host_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.example.com:5432/options_v2")
    with pytest.raises(ConfigurationError, match="not local"):
        environments.database_url()


def test_localhost_database_url_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "postgresql://options:x@127.0.0.1:5432/options_v2"
    monkeypatch.setenv("DATABASE_URL", url)
    assert environments.database_url() == url


def test_live_orders_fail_closed_even_with_runtime_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two independent conditions required; the code-level one is False all build."""
    monkeypatch.setenv("TRADING_LIVE_HUMAN_CONFIRM", "i-confirm-live")
    assert environments.live_orders_permitted() is False


def test_current_mode_is_research(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MARKET_DATA", "offline")
    assert environments.current_mode() is OperatingMode.OFFLINE_RESEARCH
    monkeypatch.setenv("TRADING_MARKET_DATA", "online")
    assert environments.current_mode() is OperatingMode.LIVE_RESEARCH
    assert environments.order_mode() == "research_only"
