"""Shared builders for Phase G position tests (not collected by pytest).

Default: a healthy open long call (SPY 600C, entry 4.50 x2, defined max loss
900, dte 16, spot 605, mark 4.50). Every override flips exactly one thing so
each test states only the condition it exercises.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from src.domain.instruments import Leg, LegSide, OptionType
from src.domain.positions import ExitPlan, Position, PositionStatus
from src.positions.exit_rules import build_exit_plan
from src.positions.monitoring import PositionMarketState
from tests.v2.gate_harness import EXPIRATION, NOW

D = Decimal
OPENED_AT = NOW - timedelta(days=5)


def make_plan(**overrides: Any) -> ExitPlan:
    kwargs: dict[str, Any] = {
        "direction": "bullish",
        "invalidation_level": D("590"),
        "max_loss_exit_usd": D("450"),
        "max_holding_days": 15,
        "long_vega": True,
        "profit_target_net_price": D("6.75"),
        "entry_theta_daily_per_unit": D("-0.05"),
        "entry_iv": D("0.30"),
    }
    kwargs.update(overrides)
    return build_exit_plan(**kwargs)


def make_position(**overrides: Any) -> Position:
    kwargs: dict[str, Any] = {
        "proposal_id": uuid.uuid4(),
        "underlying": "SPY",
        "strategy": "long_call",
        "expiration": EXPIRATION,
        "legs": (Leg(LegSide.BUY, OptionType.CALL, D("600"), 1),),
        "opened_at": OPENED_AT,
        "entry_net_price": D("4.50"),
        "quantity": 2,
        "max_loss": D("900"),
        "status": PositionStatus.OPEN,
        "exit_plan": make_plan(),
    }
    kwargs.update(overrides)
    return Position(**kwargs)


def make_spread_position(**overrides: Any) -> Position:
    kwargs: dict[str, Any] = {
        "strategy": "bull_call_debit_spread",
        "legs": (
            Leg(LegSide.BUY, OptionType.CALL, D("600"), 1),
            Leg(LegSide.SELL, OptionType.CALL, D("605"), 1),
        ),
        "entry_net_price": D("2.00"),
        "quantity": 1,
        "max_loss": D("200"),
    }
    kwargs.update(overrides)
    return make_position(**kwargs)


def make_state(**overrides: Any) -> PositionMarketState:
    kwargs: dict[str, Any] = {
        "position": make_position(),
        "as_of": NOW,
        "dte": 16,
        "spot": D("605"),
        "bid": D("4.40"),
        "ask": D("4.60"),
        "current_net_price": D("4.50"),
        "snapshot_ids": (uuid.uuid4(),),
        "current_iv": D("0.30"),
        "current_theta_daily_per_unit": D("-0.05"),
        "current_vega_per_unit": D("0.10"),
        "trend_state": "up",
    }
    kwargs.update(overrides)
    return PositionMarketState(**kwargs)
