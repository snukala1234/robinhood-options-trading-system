"""Closed-trade records — the validated raw material of every calibration.

One :class:`TradeRecord` is one completed round trip with everything the
Section 13.2 dimensions and Section 13.3 metrics need, captured at entry and
exit. Decimal-only; invalid inputs raise. :class:`FillAttempt` records order
attempts (filled or not) for fill-rate and time-to-fill metrics — unfilled
attempts are not trades and never carry P&L.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from src.config.strategy_registry import STRATEGY_REGISTRY
from src.domain.values import (
    DomainValidationError,
    require_money,
    require_non_negative_money,
    require_positive_money,
    require_symbol,
    require_utc,
)

TermStructureState = Literal["contango", "backwardation", "flat"]


@dataclass(frozen=True)
class TradeRecord:
    """One closed options trade, fully attributed for calibration."""

    trade_id: uuid.UUID
    strategy: str
    regime: str
    dte_at_entry: int
    net_delta_per_unit: Decimal  # structure delta per unit, for banding
    delta_dollars: Decimal  # total position dollar exposures at entry
    gamma_dollars: Decimal
    theta_dollars_daily: Decimal
    iv_rank: Decimal  # 0-100
    term_structure_state: TermStructureState
    spread_pct_at_entry: Decimal
    catalyst_type: str | None
    underlying: str
    sector: str
    entered_at: datetime
    exited_at: datetime
    opportunity_score: Decimal  # 0-100
    model_id: str
    prompt_version: str
    max_risk: Decimal  # defined maximum loss, dollars
    pnl_after_costs: Decimal
    costs: Decimal
    mae: Decimal  # maximum adverse excursion, dollars, >= 0
    mfe: Decimal  # maximum favorable excursion, dollars, >= 0
    slippage_vs_mid: Decimal  # dollars vs. midpoint; positive = paid worse
    predicted_win_probability: Decimal  # [0, 1], for Brier scoring

    def __post_init__(self) -> None:
        if not isinstance(self.trade_id, uuid.UUID):
            raise DomainValidationError("trade_id must be a UUID")
        if self.strategy not in STRATEGY_REGISTRY:
            raise DomainValidationError(f"unknown strategy {self.strategy!r}")
        if not self.regime or not isinstance(self.regime, str):
            raise DomainValidationError("regime must be a non-empty string")
        if (
            not isinstance(self.dte_at_entry, int)
            or isinstance(self.dte_at_entry, bool)
            or self.dte_at_entry < 0
        ):
            raise DomainValidationError("dte_at_entry must be a non-negative int")
        require_money("net_delta_per_unit", self.net_delta_per_unit)
        require_money("delta_dollars", self.delta_dollars)
        require_money("gamma_dollars", self.gamma_dollars)
        require_money("theta_dollars_daily", self.theta_dollars_daily)
        rank = require_money("iv_rank", self.iv_rank)
        if not Decimal("0") <= rank <= Decimal("100"):
            raise DomainValidationError(f"iv_rank must be in [0, 100], got {rank}")
        if self.term_structure_state not in ("contango", "backwardation", "flat"):
            raise DomainValidationError(
                f"term_structure_state invalid: {self.term_structure_state!r}"
            )
        require_non_negative_money("spread_pct_at_entry", self.spread_pct_at_entry)
        if self.catalyst_type is not None and (
            not isinstance(self.catalyst_type, str) or not self.catalyst_type
        ):
            raise DomainValidationError("catalyst_type must be None or a non-empty string")
        object.__setattr__(self, "underlying", require_symbol("underlying", self.underlying))
        if not self.sector or not isinstance(self.sector, str):
            raise DomainValidationError("sector must be a non-empty string")
        object.__setattr__(self, "entered_at", require_utc("entered_at", self.entered_at))
        object.__setattr__(self, "exited_at", require_utc("exited_at", self.exited_at))
        if self.exited_at < self.entered_at:
            raise DomainValidationError("exited_at before entered_at")
        score = require_money("opportunity_score", self.opportunity_score)
        if not Decimal("0") <= score <= Decimal("100"):
            raise DomainValidationError(f"opportunity_score must be in [0, 100], got {score}")
        if not self.model_id or not self.prompt_version:
            raise DomainValidationError("model_id and prompt_version are required")
        require_positive_money("max_risk", self.max_risk)
        require_money("pnl_after_costs", self.pnl_after_costs)
        require_non_negative_money("costs", self.costs)
        require_non_negative_money("mae", self.mae)
        require_non_negative_money("mfe", self.mfe)
        require_money("slippage_vs_mid", self.slippage_vs_mid)
        prob = require_money("predicted_win_probability", self.predicted_win_probability)
        if not Decimal("0") <= prob <= Decimal("1"):
            raise DomainValidationError(f"predicted_win_probability must be in [0, 1], got {prob}")

    @property
    def won(self) -> bool:
        return self.pnl_after_costs > 0

    @property
    def holding_days(self) -> int:
        return (self.exited_at - self.entered_at).days


@dataclass(frozen=True)
class FillAttempt:
    """One order attempt, for fill-rate and time-to-fill metrics."""

    filled: bool
    seconds_to_fill: Decimal | None = None

    def __post_init__(self) -> None:
        if self.filled:
            if self.seconds_to_fill is None:
                raise DomainValidationError("a filled attempt requires seconds_to_fill")
            require_non_negative_money("seconds_to_fill", self.seconds_to_fill)
        elif self.seconds_to_fill is not None:
            raise DomainValidationError("an unfilled attempt cannot have seconds_to_fill")
