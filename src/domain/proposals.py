"""Trade proposals — the complete options structure of Section 8.1.

A proposal is the object that competes for capital: underlying, strategy, expiration,
legs, pricing, defined risk, Greeks, and the full reasoning/exit context. Everything
monetary is ``Decimal``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

from src.config.strategy_registry import spec_for
from src.domain.instruments import Leg
from src.domain.values import (
    DomainValidationError,
    require_positive_money,
    require_symbol,
)


class Direction(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


@dataclass(frozen=True)
class TradeProposal:
    """Section 8.1 required proposal schema (analytics dicts filled by Phase C+)."""

    underlying: str
    direction: Direction
    strategy: str
    expiration: date
    dte: int
    legs: tuple[Leg, ...]
    limit_price: Decimal
    max_loss: Decimal
    max_gain: Decimal | None
    breakevens: tuple[Decimal, ...]
    net_delta: Decimal
    net_gamma: Decimal
    net_theta_daily: Decimal
    net_vega: Decimal
    config_version_id: uuid.UUID
    proposal_id: uuid.UUID = field(default_factory=uuid.uuid4)
    liquidity: dict[str, Any] = field(default_factory=dict)
    thesis: dict[str, Any] = field(default_factory=dict)
    invalidation: dict[str, Any] = field(default_factory=dict)
    exit_plan: dict[str, Any] = field(default_factory=dict)
    opportunity_score: dict[str, Any] = field(default_factory=dict)
    portfolio_impact: dict[str, Any] = field(default_factory=dict)
    risk_officer_decision: dict[str, Any] = field(default_factory=dict)
    data_snapshot_ids: tuple[uuid.UUID, ...] = ()
    model_versions: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "underlying", require_symbol("underlying", self.underlying))
        if not isinstance(self.direction, Direction):
            raise DomainValidationError("direction must be a Direction")
        spec = spec_for(self.strategy)  # KeyError -> unknown strategy is rejected
        if not self.legs:
            raise DomainValidationError("proposal must have at least one leg")
        if len(self.legs) != spec.legs:
            raise DomainValidationError(
                f"strategy {self.strategy!r} requires {spec.legs} leg(s), got {len(self.legs)}"
            )
        if not isinstance(self.dte, int) or isinstance(self.dte, bool) or self.dte < 0:
            raise DomainValidationError(f"dte must be a non-negative int, got {self.dte!r}")
        require_positive_money("limit_price", self.limit_price)
        # Defined maximum loss is mandatory (REQUIRE_DEFINED_MAX_LOSS): a proposal
        # without a computable max loss must never exist as an object.
        require_positive_money("max_loss", self.max_loss)
        if self.max_gain is not None:
            require_positive_money("max_gain", self.max_gain)
        for i, be in enumerate(self.breakevens):
            require_positive_money(f"breakevens[{i}]", be)
        for name in ("net_delta", "net_gamma", "net_theta_daily", "net_vega"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise DomainValidationError(f"{name} must be a Decimal")
        if not isinstance(self.config_version_id, uuid.UUID):
            raise DomainValidationError("config_version_id must be a UUID")
