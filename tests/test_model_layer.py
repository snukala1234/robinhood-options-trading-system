"""Model-routing + Section 3.8 failover tests, plus a no-hardcoded-model-string scan."""

from __future__ import annotations

from pathlib import Path

import pytest

from config import models
from config.models import model_chain, model_for
from core.db import Database
from core.llm import (
    AllModelsUnavailableError,
    ModelClient,
    ModelUnavailableError,
    OfflineProvider,
    TransientModelError,
    extract_json,
)


class ScriptedProvider:
    """A fake Anthropic-like provider with per-model scripted outcomes."""

    kind = "anthropic"

    def __init__(self, behavior: dict[str, list[object]]) -> None:
        self.behavior = behavior
        self.calls: list[str] = []
        self._idx: dict[str, int] = {}

    def generate(self, model: str, system: str, user: str) -> str:
        self.calls.append(model)
        i = self._idx.get(model, 0)
        self._idx[model] = i + 1
        outcomes = self.behavior.get(model, [])
        if i < len(outcomes):
            outcome = outcomes[i]
            if isinstance(outcome, Exception):
                raise outcome
            return str(outcome)
        return '{"ok": true}'


OFFLINE = {"direction": "long", "confidence": 0.7}


# === config/models =========================================================

def test_every_agent_defaults_to_fable_5() -> None:
    assert set(models.ALL_AGENT_KEYS) == set(models.AGENT_MODELS)
    for key in models.ALL_AGENT_KEYS:
        assert model_for(key) == "claude-fable-5"


def test_unknown_agent_key_raises() -> None:
    with pytest.raises(KeyError):
        model_for("does_not_exist")


def test_failover_chain_order() -> None:
    assert model_chain("scanner") == ["claude-fable-5", "claude-opus-4-8", "claude-sonnet-5"]


# === offline path ==========================================================

def test_offline_path_returns_offline_result() -> None:
    client = ModelClient(provider=OfflineProvider())
    result = client.complete_json("research_technical", "sys", "user", OFFLINE)
    assert result.provider == "offline"
    assert result.data == OFFLINE
    assert result.active_model == "claude-fable-5"
    assert result.decided_under_failover is False
    assert result.attempts == 0


# === live failover =========================================================

def test_sustained_unavailable_triggers_failover_and_is_tagged(db: Database) -> None:
    provider = ScriptedProvider(
        {
            "claude-fable-5": [ModelUnavailableError("suspended")],
            "claude-opus-4-8": ['{"direction": "long"}'],
        }
    )
    client = ModelClient(provider=provider, db=db)
    result = client.complete_json("research_macro", "sys", "user", OFFLINE)

    assert result.active_model == "claude-opus-4-8"
    assert result.decided_under_failover is True
    assert result.data == {"direction": "long"}
    # Failover event recorded to the audit trail.
    events = db.failover_events()
    assert len(events) == 1
    assert events[0]["requested_model"] == "claude-fable-5"
    assert events[0]["fell_back_to"] == "claude-opus-4-8"


def test_transient_errors_retry_same_model_without_failover() -> None:
    provider = ScriptedProvider(
        {
            "claude-fable-5": [
                TransientModelError("rate limit"),
                TransientModelError("timeout"),
                '{"direction": "flat"}',
            ]
        }
    )
    client = ModelClient(provider=provider)
    result = client.complete_json("scanner", "sys", "user", OFFLINE)
    assert result.active_model == "claude-fable-5"
    assert result.decided_under_failover is False
    assert result.attempts == 3
    assert provider.calls == ["claude-fable-5"] * 3


def test_transient_exhaustion_escalates_to_next_model(db: Database) -> None:
    # MAX_MODEL_UNAVAILABLE_RETRIES transient failures -> treat as unavailable -> failover.
    n = models.MAX_MODEL_UNAVAILABLE_RETRIES
    provider = ScriptedProvider(
        {
            "claude-fable-5": [TransientModelError("timeout")] * n,
            "claude-opus-4-8": ['{"direction": "short"}'],
        }
    )
    client = ModelClient(provider=provider, db=db)
    result = client.complete_json("edge_aggregator", "sys", "user", OFFLINE)
    assert result.active_model == "claude-opus-4-8"
    assert result.decided_under_failover is True
    assert provider.calls.count("claude-fable-5") == n


def test_all_models_unavailable_raises() -> None:
    provider = ScriptedProvider(
        {
            "claude-fable-5": [ModelUnavailableError("x")],
            "claude-opus-4-8": [ModelUnavailableError("x")],
            "claude-sonnet-5": [ModelUnavailableError("x")],
        }
    )
    client = ModelClient(provider=provider)
    with pytest.raises(AllModelsUnavailableError):
        client.complete_json("scanner", "sys", "user", OFFLINE)


# === JSON extraction =======================================================

def test_extract_json_from_fenced_prose() -> None:
    text = 'Sure! Here is the result:\n```json\n{"direction": "long", "confidence": 0.8}\n```'
    assert extract_json(text) == {"direction": "long", "confidence": 0.8}


def test_extract_json_plain() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_raises_when_absent() -> None:
    with pytest.raises(ValueError, match="no JSON object"):
        extract_json("no json here")


# === spec fidelity: no hardcoded model strings outside config/models.py ====

def test_no_model_string_hardcoded_outside_config_models() -> None:
    repo = Path(__file__).resolve().parent.parent
    allowed = {repo / "config" / "models.py"}
    offenders: list[str] = []
    for pkg in ("config", "core", "risk", "agents", "dashboard"):
        for path in (repo / pkg).rglob("*.py"):
            if path in allowed:
                continue
            if 'claude-fable-5' in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(repo)))
    # Also check top-level modules (orchestrator, run_paper) once they exist.
    for name in ("orchestrator.py", "run_paper.py"):
        p = repo / name
        if p.exists() and "claude-fable-5" in p.read_text(encoding="utf-8"):
            offenders.append(name)
    assert offenders == [], f"model id hardcoded outside config/models.py: {offenders}"
