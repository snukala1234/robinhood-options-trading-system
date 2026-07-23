"""Agent 6 (Portfolio Manager): budget/concurrency/correlation branches exact."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from src.agents.portfolio_manager import (
    AGENT_KEY,
    PROMPT_VERSION,
    REPLACEMENT_SCORE_MARGIN,
    AllocationPacket,
    PortfolioManagerAgent,
    offline_payload,
)
from src.agents.runtime import AgentRuntime
from src.config.risk_policy import MAX_CONCURRENT_POSITIONS
from src.domain.values import DomainValidationError

D = Decimal
NOW = datetime(2026, 7, 22, 14, 0, tzinfo=UTC)


def _packet(**overrides: Any) -> AllocationPacket:
    kwargs: dict[str, Any] = {
        "as_of": NOW,
        "candidate_id": "cand-1",
        "opportunity_score_total": D("85"),
        "candidate_max_loss": D("185"),
        "remaining_per_trade_budget": D("300"),
        "remaining_portfolio_budget": D("1000"),
        "settled_cash": D("500"),
        "capital_required": D("185"),
        "open_position_count": 1,
        "correlation_with_portfolio": D("0.2"),
        "weakest_open_score": None,
        "snapshot_ids": (uuid.uuid4(),),
    }
    kwargs.update(overrides)
    return AllocationPacket(**kwargs)


def test_budget_breaches_hold_cash_with_named_budget() -> None:
    per_trade = offline_payload(_packet(candidate_max_loss=D("400")))
    assert per_trade["action"] == "hold_cash"
    assert "per-trade" in per_trade["rationale"]

    portfolio = offline_payload(
        _packet(candidate_max_loss=D("250"), remaining_portfolio_budget=D("200"))
    )
    assert portfolio["action"] == "hold_cash"
    assert "portfolio" in portfolio["rationale"]

    cash = offline_payload(_packet(capital_required=D("600")))
    assert cash["action"] == "hold_cash"
    assert "settled cash" in cash["rationale"]


def test_concurrency_cap_defer_vs_replace_boundary() -> None:
    at_cap = {"open_position_count": MAX_CONCURRENT_POSITIONS}
    # Exactly +margin: replace.
    replace = offline_payload(
        _packet(
            **at_cap,
            weakest_open_score=D("85") - REPLACEMENT_SCORE_MARGIN,
        )
    )
    assert replace["action"] == "replace_existing"
    assert replace["replace_position_id"] == "weakest"
    # One point short of the margin: defer.
    defer = offline_payload(
        _packet(
            **at_cap,
            weakest_open_score=D("85") - REPLACEMENT_SCORE_MARGIN + D("1"),
        )
    )
    assert defer["action"] == "defer"
    # No weakest score known: defer.
    assert offline_payload(_packet(**at_cap))["action"] == "defer"


def test_high_correlation_halves_risk() -> None:
    payload = offline_payload(_packet(correlation_with_portfolio=D("0.7")))
    assert payload["action"] == "reduce_risk"
    assert payload["risk_fraction_of_request"] == "0.5"


def test_clean_candidate_approved_for_gate() -> None:
    payload = offline_payload(_packet())
    assert payload["action"] == "approve_for_gate"
    assert payload["risk_fraction_of_request"] == "1"


def test_budget_beats_concurrency_precedence() -> None:
    payload = offline_payload(
        _packet(
            candidate_max_loss=D("400"),
            open_position_count=MAX_CONCURRENT_POSITIONS,
            weakest_open_score=D("10"),
        )
    )
    assert payload["action"] == "hold_cash"


def test_offline_is_deterministic() -> None:
    assert offline_payload(_packet()) == offline_payload(_packet())


def test_packet_rejects_invalid_inputs() -> None:
    with pytest.raises(DomainValidationError):
        _packet(candidate_id="")
    with pytest.raises(DomainValidationError):
        _packet(opportunity_score_total=D("101"))
    with pytest.raises(DomainValidationError):
        _packet(candidate_max_loss=185.0)  # float
    with pytest.raises(DomainValidationError):
        _packet(open_position_count=-1)
    with pytest.raises(DomainValidationError):
        _packet(correlation_with_portfolio=D("1.2"))
    with pytest.raises(DomainValidationError):
        _packet(snapshot_ids=())


def test_allocate_end_to_end_logs_decision(conn: psycopg.Connection[Any]) -> None:
    agent = PortfolioManagerAgent(AgentRuntime(conn))
    corr = uuid.uuid4()
    result = agent.allocate(_packet(), correlation_id=corr)
    assert result.output.action == "approve_for_gate"
    row = conn.execute("SELECT * FROM agent_decisions").fetchone()
    assert row is not None
    assert row["agent_name"] == AGENT_KEY
    assert row["prompt_version"] == PROMPT_VERSION
    assert row["correlation_id"] == corr
