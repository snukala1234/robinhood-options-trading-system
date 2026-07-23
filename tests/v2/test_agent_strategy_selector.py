"""Agent 5 (Strategy Selector): capability gating, no-trade validity, semantic gate."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest

import src.agents.strategy_selector as selector_module
from src.agents.runtime import AgentRuntime, InvalidAgentOutput
from src.agents.strategy_selector import (
    AGENT_KEY,
    PROMPT_VERSION,
    StrategySelectionPacket,
    StrategySelectorAgent,
    offline_payload,
)
from src.domain.values import DomainValidationError

D = Decimal
NOW = datetime(2026, 7, 22, 14, 0, tzinfo=UTC)

ALL_EXECUTABLE = frozenset(
    {
        "long_call",
        "long_put",
        "bull_call_debit_spread",
        "bear_put_debit_spread",
        "put_credit_spread",
        "call_credit_spread",
    }
)
ALL_FAMILIES = ("long_premium", "debit_spreads", "credit_spreads")


def _packet(**overrides: Any) -> StrategySelectionPacket:
    kwargs: dict[str, Any] = {
        "as_of": NOW,
        "underlying": "SPY",
        "direction": "bullish",
        "conviction": D("0.7"),
        "premium_richness": "fair",
        "permitted_families": ALL_FAMILIES,
        "executable_strategies": ALL_EXECUTABLE,
        "snapshot_ids": (uuid.uuid4(),),
    }
    kwargs.update(overrides)
    return StrategySelectionPacket(**kwargs)


def test_bullish_prefers_debit_spread() -> None:
    payload = offline_payload(_packet())
    assert payload["selected_strategy"] == "bull_call_debit_spread"
    considered = {a["strategy"] for a in payload["alternatives_considered"]}
    assert "long_call" in considered  # at least two structures compared


def test_no_spread_capability_falls_back_to_long_call() -> None:
    payload = offline_payload(_packet(executable_strategies=frozenset({"long_call", "long_put"})))
    assert payload["selected_strategy"] == "long_call"
    reasons = {a["strategy"]: a["reason_rejected"] for a in payload["alternatives_considered"]}
    assert reasons["bull_call_debit_spread"] == "not executable on this account"


def test_rich_premium_excludes_long_options() -> None:
    payload = offline_payload(_packet(premium_richness="rich"))
    considered = {a["strategy"] for a in payload["alternatives_considered"]}
    assert "long_call" not in considered
    assert payload["selected_strategy"] == "bull_call_debit_spread"


def test_bearish_mirror() -> None:
    payload = offline_payload(_packet(direction="bearish"))
    assert payload["selected_strategy"] == "bear_put_debit_spread"


def test_nothing_executable_means_no_trade() -> None:
    payload = offline_payload(_packet(executable_strategies=frozenset()))
    assert payload["selected_strategy"] is None
    assert len(payload["alternatives_considered"]) >= 1
    assert "holding cash" in payload["rationale"]


def test_neutral_direction_means_no_trade() -> None:
    payload = offline_payload(_packet(direction="neutral"))
    assert payload["selected_strategy"] is None
    assert payload["alternatives_considered"][0]["reason_rejected"] == (
        "no strategy family permitted for this regime/direction"
    )


def test_offline_is_deterministic() -> None:
    assert offline_payload(_packet()) == offline_payload(_packet())


def test_packet_rejects_invalid_inputs() -> None:
    with pytest.raises(DomainValidationError):
        _packet(direction="long")
    with pytest.raises(DomainValidationError):
        _packet(conviction=D("1.5"))
    with pytest.raises(DomainValidationError):
        _packet(conviction=0.7)  # float
    with pytest.raises(DomainValidationError):
        _packet(executable_strategies=frozenset({"iron_condor"}))
    with pytest.raises(DomainValidationError):
        _packet(permitted_families=("naked_options",))
    with pytest.raises(DomainValidationError):
        _packet(snapshot_ids=())


def test_semantic_gate_rejects_unexecutable_selection(
    conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Schema-valid but unexecutable selection must fail closed, never propagate."""
    packet = _packet(executable_strategies=frozenset({"long_put"}))
    monkeypatch.setattr(
        selector_module,
        "offline_payload",
        lambda _p: {
            "selected_strategy": "long_call",  # registry-valid, but not executable
            "alternatives_considered": [{"strategy": "long_put", "reason_rejected": "x"}],
            "rationale": "bad",
        },
    )
    agent = StrategySelectorAgent(AgentRuntime(conn))
    with pytest.raises(InvalidAgentOutput, match="not executable"):
        agent.select(packet, correlation_id=uuid.uuid4())


def test_select_end_to_end_logs_decision(conn: psycopg.Connection[Any]) -> None:
    agent = StrategySelectorAgent(AgentRuntime(conn))
    corr = uuid.uuid4()
    result = agent.select(_packet(), correlation_id=corr)
    assert result.output.selected_strategy == "bull_call_debit_spread"
    row = conn.execute("SELECT * FROM agent_decisions").fetchone()
    assert row is not None
    assert row["agent_name"] == AGENT_KEY
    assert row["prompt_version"] == PROMPT_VERSION
    assert row["correlation_id"] == corr
