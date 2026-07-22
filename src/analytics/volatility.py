"""Volatility service (spec 5.4): realized vol, IV comparisons, term structure,
skew, expected move — pure Decimal (``Decimal.ln``/``Decimal.sqrt``), no floats.

Units: volatilities are annualized fractions (0.20 = 20%). Expected move is in
underlying price units. ``dte`` is calendar days; annualization uses 252 trading
days for realized vol and 365 calendar days for the expected-move horizon.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from src.domain.values import DomainValidationError, require_positive_money

_SIX_DP = Decimal("0.000001")

MIN_REALIZED_VOL_OBSERVATIONS = 10


def realized_volatility(
    closes: Sequence[Decimal],
    *,
    trading_days_per_year: int = 252,
    min_observations: int = MIN_REALIZED_VOL_OBSERVATIONS,
) -> Decimal:
    """Annualized close-to-close realized volatility (sample stdev of log returns).

    Rejects fewer than ``min_observations`` returns or any non-positive price.
    """
    if len(closes) < min_observations + 1:
        raise DomainValidationError(f"need >= {min_observations + 1} closes, got {len(closes)}")
    for i, c in enumerate(closes):
        require_positive_money(f"closes[{i}]", c)

    returns = [(closes[i] / closes[i - 1]).ln() for i in range(1, len(closes))]
    n = len(returns)
    mean = sum(returns, Decimal("0")) / n
    variance = sum(((r - mean) ** 2 for r in returns), Decimal("0")) / (n - 1)
    daily = variance.sqrt()
    return (daily * Decimal(trading_days_per_year).sqrt()).quantize(_SIX_DP)


def iv_realized_spread(implied: Decimal, realized: Decimal) -> Decimal:
    """Implied minus realized (positive = options priced rich vs. recent movement)."""
    require_positive_money("implied", implied)
    if realized < 0:
        raise DomainValidationError(f"realized must be >= 0, got {realized}")
    return implied - realized


@dataclass(frozen=True)
class TermPoint:
    """ATM implied volatility at one expiration."""

    expiration: date
    dte: int
    atm_iv: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.expiration, date):
            raise DomainValidationError("expiration must be a date")
        if isinstance(self.dte, bool) or not isinstance(self.dte, int) or self.dte < 0:
            raise DomainValidationError(f"dte must be an int >= 0, got {self.dte!r}")
        require_positive_money("atm_iv", self.atm_iv)


@dataclass(frozen=True)
class TermStructure:
    points: tuple[TermPoint, ...]
    near_iv: Decimal
    far_iv: Decimal
    slope: Decimal  # far minus near
    shape: str  # "contango" | "backwardation" | "flat"


#: |far - near| at or below this is classified flat rather than a real slope.
FLAT_TOLERANCE = Decimal("0.005")


def term_structure(points: Sequence[TermPoint]) -> TermStructure:
    """Classify the IV term structure across expirations (needs >= 2 points)."""
    if len(points) < 2:
        raise DomainValidationError("term structure needs >= 2 expirations")
    ordered = tuple(sorted(points, key=lambda p: p.dte))
    dtes = [p.dte for p in ordered]
    if len(set(dtes)) != len(dtes):
        raise DomainValidationError("duplicate DTE in term structure points")
    near, far = ordered[0].atm_iv, ordered[-1].atm_iv
    slope = far - near
    if abs(slope) <= FLAT_TOLERANCE:
        shape = "flat"
    elif slope > 0:
        shape = "contango"
    else:
        shape = "backwardation"
    return TermStructure(points=ordered, near_iv=near, far_iv=far, slope=slope, shape=shape)


def put_call_skew(put_iv: Decimal, call_iv: Decimal) -> Decimal:
    """Put IV minus call IV at comparable deltas (positive = downside priced richer)."""
    require_positive_money("put_iv", put_iv)
    require_positive_money("call_iv", call_iv)
    return put_iv - call_iv


def expected_move(spot: Decimal, iv: Decimal, dte: int) -> Decimal:
    """One-standard-deviation expected move by expiration: spot * iv * sqrt(dte/365).

    Clearly an estimate under a lognormal assumption; callers must not present it
    as a bound.
    """
    require_positive_money("spot", spot)
    require_positive_money("iv", iv)
    if isinstance(dte, bool) or not isinstance(dte, int) or dte < 1:
        raise DomainValidationError(f"dte must be an int >= 1, got {dte!r}")
    factor = (Decimal(dte) / Decimal(365)).sqrt()
    return (spot * iv * factor).quantize(Decimal("0.01"))
