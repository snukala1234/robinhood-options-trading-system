"""Positions and the five-dimension exit plan (Section 10)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from src.domain.instruments import Leg
from src.domain.values import (
    DomainValidationError,
    require_optional_money,
    require_positive_int,
    require_positive_money,
    require_symbol,
    require_utc,
)

#: The five mandatory exit dimensions (Section 10). An ExitPlan missing any of these
#: cannot be constructed, so a position without a complete plan cannot exist.
EXIT_DIMENSIONS = ("premium", "underlying", "time", "volatility", "event")


@dataclass(frozen=True)
class ExitPlan:
    """Exit conditions across all five Section 10 dimensions."""

    premium: dict[str, Any]
    underlying: dict[str, Any]
    time: dict[str, Any]
    volatility: dict[str, Any]
    event: dict[str, Any]

    def __post_init__(self) -> None:
        for dim in EXIT_DIMENSIONS:
            value = getattr(self, dim)
            if not isinstance(value, dict) or not value:
                raise DomainValidationError(
                    f"exit plan dimension {dim!r} must be a non-empty dict (Section 10)"
                )

    def to_dict(self) -> dict[str, dict[str, Any]]:
        return {dim: dict(getattr(self, dim)) for dim in EXIT_DIMENSIONS}


class PositionStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


@dataclass(frozen=True)
class Position:
    """An open or closed options position (maps to the Section 14 positions table)."""

    proposal_id: uuid.UUID
    underlying: str
    strategy: str
    expiration: date
    legs: tuple[Leg, ...]
    opened_at: datetime
    entry_net_price: Decimal
    quantity: int
    max_loss: Decimal
    status: PositionStatus
    exit_plan: ExitPlan
    closed_at: datetime | None = None
    exit_net_price: Decimal | None = None
    position_id: uuid.UUID = field(default_factory=uuid.uuid4)

    def __post_init__(self) -> None:
        if not isinstance(self.proposal_id, uuid.UUID):
            raise DomainValidationError("proposal_id must be a UUID")
        object.__setattr__(self, "underlying", require_symbol("underlying", self.underlying))
        if not self.legs:
            raise DomainValidationError("position must have at least one leg")
        object.__setattr__(self, "opened_at", require_utc("opened_at", self.opened_at))
        require_positive_money("entry_net_price", self.entry_net_price)
        require_positive_int("quantity", self.quantity)
        require_positive_money("max_loss", self.max_loss)
        if not isinstance(self.status, PositionStatus):
            raise DomainValidationError("status must be a PositionStatus")
        if not isinstance(self.exit_plan, ExitPlan):
            raise DomainValidationError("exit_plan must be an ExitPlan (all 5 dimensions)")
        require_optional_money("exit_net_price", self.exit_net_price)
        if self.closed_at is not None:
            object.__setattr__(self, "closed_at", require_utc("closed_at", self.closed_at))
        if self.status is PositionStatus.CLOSED and self.closed_at is None:
            raise DomainValidationError("closed position requires closed_at")
