"""Portfolio snapshots (Section 14 portfolio_snapshots table shape)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from src.domain.values import (
    DomainValidationError,
    require_non_negative_money,
    require_optional_money,
    require_utc,
)


@dataclass(frozen=True)
class PortfolioSnapshot:
    """Point-in-time account and Greek exposure state. ``is_paper`` defaults True."""

    observed_at: datetime
    total_equity: Decimal
    settled_cash: Decimal
    unsettled_cash: Decimal
    open_risk: Decimal
    net_delta: Decimal | None = None
    net_gamma: Decimal | None = None
    daily_theta: Decimal | None = None
    net_vega: Decimal | None = None
    high_water_mark: Decimal | None = None
    drawdown: Decimal | None = None
    is_paper: bool = True
    snapshot_id: uuid.UUID = field(default_factory=uuid.uuid4)

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", require_utc("observed_at", self.observed_at))
        require_non_negative_money("total_equity", self.total_equity)
        require_non_negative_money("settled_cash", self.settled_cash)
        require_non_negative_money("unsettled_cash", self.unsettled_cash)
        require_non_negative_money("open_risk", self.open_risk)
        for name in (
            "net_delta",
            "net_gamma",
            "daily_theta",
            "net_vega",
            "high_water_mark",
            "drawdown",
        ):
            require_optional_money(name, getattr(self, name))
        if not isinstance(self.is_paper, bool):
            raise DomainValidationError("is_paper must be a bool")
