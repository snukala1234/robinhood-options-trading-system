"""Agent 8 (Position Manager): recommendation precedence; exits stay pure code."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from src.agents.position_manager import (
    AGENT_KEY,
    PROMPT_VERSION,
    PositionManagerAgent,
    PositionReviewPacket,
    offline_payload,
)
from src.agents.runtime import REQUIRED_ENTRY_AGENTS, AgentRuntime
from src.config.tunables import DEFAULT_TUNABLES
from src.domain.values import DomainValidationError

D = Decimal
NOW = datetime(2026, 7, 22, 14, 0, tzinfo=UTC)


def _packet(**overrides: Any) -> PositionReviewPacket:
    kwargs: dict[str, Any] = {
        "as_of": NOW,
        "underlying": "SPY",
        "strategy": "bull_call_debit_spread",
        "dte": 14,
        "unrealized_pnl_fraction_of_max_gain": D("0.2"),
        "unrealized_pnl_fraction_of_max_loss": D("0"),
        "thesis_intact": True,
        "iv_change_since_entry": D("0.01"),
        "event_before_expiration": False,
        "snapshot_ids": (uuid.uuid4(),),
    }
    kwargs.update(overrides)
    return PositionReviewPacket(**kwargs)


def test_forced_dte_exit_beats_everything() -> None:
    payload = offline_payload(
        _packet(
            dte=DEFAULT_TUNABLES.dte_forced_exit,
            unrealized_pnl_fraction_of_max_gain=D("0.9"),  # even a big winner
            thesis_intact=True,
        )
    )
    assert payload["action"] == "exit" and payload["urgency"] == "high"
    assert "forced DTE" in payload["rationale"]


def test_broken_thesis_exits() -> None:
    payload = offline_payload(_packet(thesis_intact=False))
    assert payload["action"] == "exit" and payload["urgency"] == "high"


def test_profit_target_takes_profit() -> None:
    payload = offline_payload(_packet(unrealized_pnl_fraction_of_max_gain=D("0.5")))
    assert payload["action"] == "take_profit"


def test_event_before_expiration_exits() -> None:
    payload = offline_payload(_packet(event_before_expiration=True))
    assert payload["action"] == "exit"
    assert "catalyst" in payload["rationale"]


def test_dte_checkpoint_reduces() -> None:
    payload = offline_payload(_packet(dte=DEFAULT_TUNABLES.dte_review_checkpoint))
    assert payload["action"] == "reduce"


def test_healthy_position_holds_with_conditions() -> None:
    payload = offline_payload(_packet())
    assert payload["action"] == "hold" and payload["urgency"] == "low"
    assert payload["conditions"]  # what would change the answer is explicit


def test_offline_is_deterministic() -> None:
    assert offline_payload(_packet()) == offline_payload(_packet())


def test_position_manager_is_not_entry_required() -> None:
    """Its outage must never block anything: exits are pure code (Section 10.6)."""
    assert AGENT_KEY not in REQUIRED_ENTRY_AGENTS


def test_packet_rejects_invalid_inputs() -> None:
    with pytest.raises(DomainValidationError):
        _packet(dte=-1)
    with pytest.raises(DomainValidationError):
        _packet(unrealized_pnl_fraction_of_max_gain=D("1.5"))
    with pytest.raises(DomainValidationError):
        _packet(unrealized_pnl_fraction_of_max_gain=0.5)  # float
    with pytest.raises(DomainValidationError):
        _packet(strategy="iron_condor")
    with pytest.raises(DomainValidationError):
        _packet(snapshot_ids=())


def test_evaluate_position_end_to_end_logs_decision(
    conn: psycopg.Connection[Any],
) -> None:
    agent = PositionManagerAgent(AgentRuntime(conn))
    corr = uuid.uuid4()
    result = agent.evaluate_position(_packet(dte=2), correlation_id=corr)
    assert result.output.action == "exit"
    row = conn.execute("SELECT * FROM agent_decisions").fetchone()
    assert row is not None
    assert row["agent_name"] == AGENT_KEY
    assert row["prompt_version"] == PROMPT_VERSION
    assert row["correlation_id"] == corr
