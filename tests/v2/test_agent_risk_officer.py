"""Agent 7 (Risk Officer): veto precedence and reduction coherence."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from src.agents.risk_officer import (
    AGENT_KEY,
    PROMPT_VERSION,
    RiskOfficerAgent,
    RiskReviewPacket,
    offline_payload,
)
from src.agents.runtime import AgentRuntime
from src.domain.values import DomainValidationError

D = Decimal
NOW = datetime(2026, 7, 22, 14, 0, tzinfo=UTC)


def _packet(**overrides: Any) -> RiskReviewPacket:
    kwargs: dict[str, Any] = {
        "as_of": NOW,
        "underlying": "SPY",
        "strategy": "bull_call_debit_spread",
        "max_loss": D("185"),
        "breached_limit_names": (),
        "liquidity_failures": (),
        "earnings_before_expiration": False,
        "event_risk": "low",
        "correlation_with_portfolio": D("0.2"),
        "thesis_conviction": D("0.75"),
        "snapshot_ids": (uuid.uuid4(),),
    }
    kwargs.update(overrides)
    return RiskReviewPacket(**kwargs)


def test_breached_limits_always_veto() -> None:
    payload = offline_payload(
        _packet(breached_limit_names=("total_open_risk_pct", "underlying_risk_pct:SPY"))
    )
    assert payload["decision"] == "veto"
    assert payload["reasons"] == [
        "limit breached: total_open_risk_pct",
        "limit breached: underlying_risk_pct:SPY",
    ]


def test_limit_breach_beats_everything_else() -> None:
    payload = offline_payload(
        _packet(
            breached_limit_names=("x",),
            earnings_before_expiration=True,
            liquidity_failures=("wide spread",),
        )
    )
    assert payload["decision"] == "veto"
    assert payload["reasons"] == ["limit breached: x"]


def test_earnings_inside_window_vetoes() -> None:
    payload = offline_payload(_packet(earnings_before_expiration=True))
    assert payload["decision"] == "veto"
    assert "ALLOW_EARNINGS_HOLD" in payload["reasons"][0]


def test_liquidity_failures_veto_with_their_reasons() -> None:
    payload = offline_payload(_packet(liquidity_failures=("open_interest 50 < 100",)))
    assert payload["decision"] == "veto"
    assert payload["reasons"] == ["open_interest 50 < 100"]


def test_high_event_risk_moderate_conviction_reduces() -> None:
    payload = offline_payload(_packet(event_risk="high", thesis_conviction=D("0.6")))
    assert payload["decision"] == "approve_with_reduction"
    assert payload["reduction_fraction"] == "0.5"
    # Strong conviction rides through high event risk without reduction.
    strong = offline_payload(_packet(event_risk="high", thesis_conviction=D("0.7")))
    assert strong["decision"] == "approve"


def test_crowding_reduces() -> None:
    payload = offline_payload(_packet(correlation_with_portfolio=D("0.7")))
    assert payload["decision"] == "approve_with_reduction"
    assert payload["reasons"] == ["crowded exposure vs existing positions"]


def test_clean_proposal_approved() -> None:
    payload = offline_payload(_packet())
    assert payload["decision"] == "approve"
    assert "reduction_fraction" not in payload  # coherent: only with reductions


def test_offline_is_deterministic() -> None:
    assert offline_payload(_packet()) == offline_payload(_packet())


def test_packet_rejects_invalid_inputs() -> None:
    with pytest.raises(DomainValidationError):
        _packet(strategy="iron_condor")
    with pytest.raises(DomainValidationError):
        _packet(max_loss=D("0"))
    with pytest.raises(DomainValidationError):
        _packet(event_risk="extreme")
    with pytest.raises(DomainValidationError):
        _packet(thesis_conviction=0.75)  # float
    with pytest.raises(DomainValidationError):
        _packet(snapshot_ids=())


def test_review_end_to_end_logs_decision(conn: psycopg.Connection[Any]) -> None:
    agent = RiskOfficerAgent(AgentRuntime(conn))
    corr = uuid.uuid4()
    result = agent.review(_packet(earnings_before_expiration=True), correlation_id=corr)
    assert result.output.decision == "veto"
    row = conn.execute("SELECT * FROM agent_decisions").fetchone()
    assert row is not None
    assert row["agent_name"] == AGENT_KEY
    assert row["prompt_version"] == PROMPT_VERSION
    assert row["correlation_id"] == corr
