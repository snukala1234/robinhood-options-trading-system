"""Section 16 model routing: alias resolution, failover, and the no-hardcoding sweep."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import models

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


def test_all_nine_agents_have_a_model() -> None:
    assert set(models.ALL_AGENT_KEYS) == {
        "market_regime",
        "catalyst_research",
        "technical_analyst",
        "options_specialist",
        "strategy_selector",
        "portfolio_manager",
        "risk_officer",
        "position_manager",
        "performance_auditor",
    }
    for key in models.ALL_AGENT_KEYS:
        assert models.model_for(key)


def test_unknown_agent_key_raises() -> None:
    with pytest.raises(KeyError):
        models.model_for("nonexistent_agent")


def test_env_alias_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(models.REASONING_ALIAS, "claude-test-override")
    assert models.model_for("risk_officer") == "claude-test-override"
    # Balanced agents are unaffected by the reasoning override.
    assert models.model_for("technical_analyst") != "claude-test-override"


def test_model_chain_walks_failover_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain = models.model_chain("market_regime")
    assert chain[0] == models.model_for("market_regime")
    assert len(chain) == len(set(chain))
    # If both aliases resolve to the same ID, the chain collapses to one entry.
    monkeypatch.setenv(models.REASONING_ALIAS, "claude-same")
    monkeypatch.setenv(models.BALANCED_ALIAS, "claude-same")
    assert models.model_chain("market_regime") == ["claude-same"]


def test_no_model_id_hardcoded_outside_models_config() -> None:
    """Rule 12 of the master prompt, scoped to the V2 tree (V1 has its own test)."""
    offenders: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        if path.name == "models.py" and path.parent.name == "config":
            continue
        if "claude-" in path.read_text(encoding="utf-8"):
            offenders.append(str(path))
    assert offenders == []
