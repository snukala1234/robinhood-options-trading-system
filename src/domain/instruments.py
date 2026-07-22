"""Option contracts and legs (Section 8.1 leg schema)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from src.domain.values import (
    DomainValidationError,
    require_positive_int,
    require_positive_money,
    require_symbol,
)


class OptionType(StrEnum):
    CALL = "call"
    PUT = "put"


class LegSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class OptionContract:
    """One listed option contract."""

    underlying: str
    expiration: date
    strike: Decimal
    option_type: OptionType
    multiplier: int = 100

    def __post_init__(self) -> None:
        object.__setattr__(self, "underlying", require_symbol("underlying", self.underlying))
        if not isinstance(self.expiration, date) or isinstance(self.expiration, bool):
            raise DomainValidationError("expiration must be a date")
        require_positive_money("strike", self.strike)
        require_positive_int("multiplier", self.multiplier)
        if not isinstance(self.option_type, OptionType):
            raise DomainValidationError("option_type must be an OptionType")

    def occ_symbol(self) -> str:
        """OCC-style option symbol (e.g. ``SPY260918C00600000``)."""
        yymmdd = self.expiration.strftime("%y%m%d")
        cp = "C" if self.option_type is OptionType.CALL else "P"
        thousandths = int(self.strike * 1000)
        return f"{self.underlying}{yymmdd}{cp}{thousandths:08d}"


@dataclass(frozen=True)
class Leg:
    """One leg of a strategy (Section 8.1: side, type, strike, quantity)."""

    side: LegSide
    option_type: OptionType
    strike: Decimal
    quantity: int

    def __post_init__(self) -> None:
        if not isinstance(self.side, LegSide):
            raise DomainValidationError("side must be a LegSide")
        if not isinstance(self.option_type, OptionType):
            raise DomainValidationError("option_type must be an OptionType")
        require_positive_money("strike", self.strike)
        require_positive_int("quantity", self.quantity)
