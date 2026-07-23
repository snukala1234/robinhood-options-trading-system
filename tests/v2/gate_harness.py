"""Shared builders for Phase F gate tests (not collected by pytest).

The default input is a fully green long-call entry: every builder override
flips exactly one thing, so each test states only the attack it performs.
Numbers (equity 100k, settled 50k, 4.50 debit on a 100-multiplier call):
per-trade budget 1000 -> quantity 2, total max loss 900, total debit 900.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from src.agents.schemas import PortfolioManagerDecision, RiskOfficerDecision
from src.analytics.portfolio_exposure import PortfolioExposure, aggregate
from src.data.option_chains import ContractQuote
from src.domain.instruments import Leg, LegSide, OptionContract, OptionType
from src.domain.proposals import Direction, TradeProposal
from src.execution.interface import AccountSnapshot
from src.execution.paper_broker import PAPER_CAPABILITIES
from src.gate.trade_gate import CircuitBreakerInputs, GateInput
from src.risk.settlement import CashAccountState

D = Decimal
NOW = datetime(2026, 7, 22, 15, 0, tzinfo=UTC)
EXPIRATION = date(2026, 8, 7)  # dte 16 from NOW
EQUITY = D("100000")
SETTLED = D("50000")

CALL_600 = OptionContract(
    underlying="SPY", expiration=EXPIRATION, strike=D("600"), option_type=OptionType.CALL
)

PM_APPROVE = PortfolioManagerDecision(action="approve_for_gate", rationale="fits the book")
RO_APPROVE = RiskOfficerDecision(decision="approve", reasons=["within all limits"])


def make_proposal(**overrides: Any) -> TradeProposal:
    kwargs: dict[str, Any] = {
        "underlying": "SPY",
        "direction": Direction.BULLISH,
        "strategy": "long_call",
        "expiration": EXPIRATION,
        "dte": 16,
        "legs": (Leg(LegSide.BUY, OptionType.CALL, D("600"), 1),),
        "limit_price": D("4.50"),
        "max_loss": D("450"),  # per unit, dollars
        "max_gain": None,
        "breakevens": (D("604.50"),),
        "net_delta": D("0.40"),
        "net_gamma": D("0.02"),
        "net_theta_daily": D("-0.05"),
        "net_vega": D("0.10"),
        "config_version_id": uuid.uuid4(),
    }
    kwargs.update(overrides)
    return TradeProposal(**kwargs)


def make_quote(**overrides: Any) -> ContractQuote:
    kwargs: dict[str, Any] = {
        "contract": CALL_600,
        "bid": D("4.40"),
        "ask": D("4.60"),
        "midpoint": D("4.50"),
        "volume": 100,
        "open_interest": 500,
        "observed_at": NOW,
        "received_at": NOW,
        "source": "test",
    }
    kwargs.update(overrides)
    return ContractQuote(**kwargs)


def make_account(**overrides: Any) -> AccountSnapshot:
    kwargs: dict[str, Any] = {
        "account_id_hash": "test-account-hash",
        "total_equity": EQUITY,
        "settled_cash": SETTLED,
        "unsettled_cash": D("0"),
        "observed_at": NOW,
    }
    kwargs.update(overrides)
    return AccountSnapshot(**kwargs)


def empty_portfolio(equity: Decimal = EQUITY) -> PortfolioExposure:
    return aggregate((), account_equity=equity)


NO_BREACHES = CircuitBreakerInputs(D("0"), D("0"), D("0"), D("0"))


def make_input(**overrides: Any) -> GateInput:
    kwargs: dict[str, Any] = {
        "proposal": make_proposal(),
        "pm_decision": PM_APPROVE,
        "ro_decision": RO_APPROVE,
        "decided_under_failover": False,
        "account": make_account(),
        "cash_state": CashAccountState(settled_cash=SETTLED),
        "capabilities": PAPER_CAPABILITIES,
        "leg_quotes": (make_quote(),),
        "quote_snapshot_ids": (uuid.uuid4(),),
        "underlying_data_age_seconds": 1.0,
        "portfolio": empty_portfolio(),
        "open_position_count": 0,
        "breakers": NO_BREACHES,
        "reconciliation_blocked_reasons": (),
        "earnings_before_expiration": False,
        "correlation_id": uuid.uuid4(),
    }
    kwargs.update(overrides)
    return GateInput(**kwargs)
