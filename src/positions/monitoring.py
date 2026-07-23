"""Validated per-position market state — the input every exit rule consumes.

A :class:`PositionMarketState` is a point-in-time, fully validated view of one
open position: current structure mark, spot, Greeks, volatility, event and
broker-state flags. Exit rules and emergency checks read ONLY this object —
they never fetch data themselves and never accept an unvalidated number.
Money is Decimal; floats are rejected at construction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from src.domain.positions import Position, PositionStatus
from src.domain.values import (
    DomainValidationError,
    require_non_negative_money,
    require_optional_money,
    require_positive_int,
    require_positive_money,
    require_utc,
)
from src.execution.interface import NetIntent
from src.gate.trade_gate import net_intent_for

TrendState = Literal["up", "down", "sideways"]


@dataclass(frozen=True)
class PositionMarketState:
    """One open position plus everything current the exit rules need."""

    position: Position
    as_of: datetime
    dte: int
    spot: Decimal
    bid: Decimal  # structure bid (net, per share)
    ask: Decimal  # structure ask (net, per share)
    current_net_price: Decimal  # structure mark (net, per share)
    snapshot_ids: tuple[uuid.UUID, ...]
    multiplier: int = 100
    current_iv: Decimal | None = None
    current_theta_daily_per_unit: Decimal | None = None
    current_vega_per_unit: Decimal | None = None
    trend_state: TrendState = "sideways"
    vol_regime_changed: bool = False
    trading_halted: bool = False
    catalyst_completed: bool = False
    new_material_event: bool = False
    next_scheduled_event_date: date | None = None
    short_leg_itm: bool = False
    assignment_notice: bool = False
    state_mismatch: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.position, Position):
            raise DomainValidationError("position must be a Position")
        if self.position.status is not PositionStatus.OPEN:
            raise DomainValidationError("exit rules only evaluate OPEN positions")
        object.__setattr__(self, "as_of", require_utc("as_of", self.as_of))
        if not isinstance(self.dte, int) or isinstance(self.dte, bool) or self.dte < 0:
            raise DomainValidationError(f"dte must be a non-negative int, got {self.dte!r}")
        require_positive_money("spot", self.spot)
        require_non_negative_money("bid", self.bid)
        require_positive_money("ask", self.ask)
        if self.bid > self.ask:
            raise DomainValidationError(
                f"crossed structure market: bid {self.bid} > ask {self.ask}"
            )
        require_non_negative_money("current_net_price", self.current_net_price)
        if not self.snapshot_ids:
            raise DomainValidationError("snapshot_ids must not be empty")
        require_positive_int("multiplier", self.multiplier)
        if self.current_iv is not None:
            require_positive_money("current_iv", self.current_iv)
        require_optional_money("current_theta_daily_per_unit", self.current_theta_daily_per_unit)
        require_optional_money("current_vega_per_unit", self.current_vega_per_unit)
        if self.trend_state not in ("up", "down", "sideways"):
            raise DomainValidationError(f"trend_state invalid: {self.trend_state!r}")
        if self.next_scheduled_event_date is not None and not isinstance(
            self.next_scheduled_event_date, date
        ):
            raise DomainValidationError("next_scheduled_event_date must be a date")

    @property
    def opened_intent(self) -> NetIntent:
        return net_intent_for(self.position.strategy)

    @property
    def holding_days(self) -> int:
        return (self.as_of - self.position.opened_at).days

    @property
    def unrealized_pnl_total(self) -> Decimal:
        """Dollars, whole position. Debit: mark up = gain; credit: mark down = gain."""
        per_share = (
            self.current_net_price - self.position.entry_net_price
            if self.opened_intent is NetIntent.DEBIT
            else self.position.entry_net_price - self.current_net_price
        )
        return per_share * self.multiplier * self.position.quantity

    @property
    def unrealized_loss_total(self) -> Decimal:
        """Non-negative loss in dollars (zero when the position is profitable)."""
        return max(-self.unrealized_pnl_total, Decimal("0"))
