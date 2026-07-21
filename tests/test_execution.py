"""Tests for Agent 6 (execution). Proves no reachable live-order confirmation path."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agents.execution import (
    STATUS_BLOCKED,
    STATUS_FILLED,
    STATUS_PENDING_APPROVAL,
    ExecutionAgent,
    OrderRequest,
    PaperBroker,
    RobinhoodMCPBroker,
)
from config.guardrails import HARD_STOP_LOSS_PCT
from core.db import Database
from core.records import Position
from risk.sizing import Account, Sale

NOW = datetime(2026, 7, 5, 15, 0, tzinfo=UTC)


class StubLiveBroker:
    """Live-like broker: can stage + submit, but has NO fill/confirm capability."""

    name = "stub_live"

    def __init__(self) -> None:
        self.staged: list[OrderRequest] = []
        self.submitted: list[OrderRequest] = []

    def stage(self, order: OrderRequest) -> str:
        self.staged.append(order)
        return "live-ref"

    def submit_for_approval(self, order: OrderRequest, broker_ref: str) -> None:
        self.submitted.append(order)


def settled(amount: float) -> Account:
    return Account(cleared_deposits=amount, recent_sales=[])


def position() -> Position:
    return Position("t1", "AAPL", 100.0, 0.5, 50.0, "2026-07-05T00:00:00+00:00",
                    HARD_STOP_LOSS_PCT, 0.25)


# === paper mode ============================================================

def test_paper_entry_stages_and_fills(db: Database) -> None:
    agent = ExecutionAgent(broker=PaperBroker(), db=db, paper=True)
    result = agent.place_entry("AAPL", shares=0.5, notional_usd=50.0, estimated_price=100.0,
                               account=settled(100.0), now=NOW)
    assert result.status == STATUS_FILLED
    assert result.fill is not None and result.fill.price == 100.0
    orders = db.recent_orders()
    assert len(orders) == 1  # single row, status advanced to filled
    assert orders[0]["status"] == STATUS_FILLED
    assert orders[0]["is_paper"] == 1
    assert orders[0]["approval_mode"] == "manual"


def test_paper_entry_blocked_when_unsettled_funds(db: Database) -> None:
    account = Account(
        cleared_deposits=10.0,
        recent_sales=[Sale(proceeds=100.0, settlement_date=(NOW + timedelta(days=1)).date())],
    )
    agent = ExecutionAgent(broker=PaperBroker(), db=db, paper=True)
    result = agent.place_entry("AAPL", shares=1.0, notional_usd=90.0, estimated_price=90.0,
                               account=account, now=NOW)
    assert result.status == STATUS_BLOCKED
    assert result.fill is None
    assert result.reason == "would_use_unsettled_funds"


def test_paper_exit_fills(db: Database) -> None:
    agent = ExecutionAgent(broker=PaperBroker(), db=db, paper=True)
    result = agent.place_exit(position(), current_price=110.0, now=NOW)
    assert result.status == STATUS_FILLED
    assert result.side == "sell"
    assert result.fill is not None and result.fill.shares == 0.5


# === live-mode safety ======================================================

def test_live_mode_never_confirms_only_awaits_robinhood(db: Database) -> None:
    broker = StubLiveBroker()
    agent = ExecutionAgent(broker=broker, db=db, paper=False)
    result = agent.place_entry("AAPL", shares=0.5, notional_usd=50.0, estimated_price=100.0,
                               account=settled(100.0), now=NOW)
    # Staged + submitted, but NEVER filled by code — the human approves on Robinhood.
    assert result.status == STATUS_PENDING_APPROVAL
    assert result.fill is None
    assert len(broker.staged) == 1 and len(broker.submitted) == 1


def test_live_broker_has_no_confirm_or_fill_method() -> None:
    # Structural guarantee: the live broker cannot confirm/fill an order at all.
    assert not hasattr(RobinhoodMCPBroker, "simulate_fill")
    assert not hasattr(RobinhoodMCPBroker, "confirm")
    assert not hasattr(RobinhoodMCPBroker, "approve")


def test_live_broker_requires_transport_and_account() -> None:
    with pytest.raises(RuntimeError):
        RobinhoodMCPBroker(mcp_transport=None, agentic_account_id="")


def test_live_broker_staging_disabled_in_paper_build() -> None:
    broker = RobinhoodMCPBroker(mcp_transport=object(), agentic_account_id="acct-123")
    with pytest.raises(RuntimeError, match="disabled in the paper build"):
        broker.stage(OrderRequest("AAPL", "buy", 1.0, 100.0, 100.0))
