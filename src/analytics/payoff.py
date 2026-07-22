"""Expiry payoff, breakevens, maximum gain/loss (spec 5.3) — pure Decimal.

Sign conventions, defined once:

- ``net_premium`` is **per share**: positive = net debit paid, negative = net credit
  received.
- Leg quantities are the ratio within one unit of the structure; ``quantity`` is the
  number of structure units; ``multiplier`` converts per-share P&L to dollars.
- Returned ``max_loss``/``max_gain`` are total dollars for the whole position.

A structure whose loss is unbounded (net short calls) is **rejected**, not analyzed:
``REQUIRE_DEFINED_MAX_LOSS`` is policy and this module enforces it at the math level.
Unbounded *gain* (a long call) is fine and reported as ``max_gain=None``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.domain.instruments import Leg, LegSide, OptionType
from src.domain.values import (
    DomainValidationError,
    require_money,
    require_positive_int,
)

_FOUR_DP = Decimal("0.0001")


@dataclass(frozen=True)
class PayoffProfile:
    """Defined-risk payoff summary in total dollars (except per-share breakevens)."""

    net_premium_per_share: Decimal
    max_loss: Decimal
    max_gain: Decimal | None
    breakevens: tuple[Decimal, ...]
    unbounded_gain: bool


def _intrinsic(leg: Leg, underlying_price: Decimal) -> Decimal:
    if leg.option_type is OptionType.CALL:
        return max(underlying_price - leg.strike, Decimal("0"))
    return max(leg.strike - underlying_price, Decimal("0"))


def _pnl_per_share(
    legs: tuple[Leg, ...], underlying_price: Decimal, net_premium: Decimal
) -> Decimal:
    total = Decimal("0")
    for leg in legs:
        signed = leg.quantity if leg.side is LegSide.BUY else -leg.quantity
        total += signed * _intrinsic(leg, underlying_price)
    return total - net_premium


def payoff_at_expiry(
    legs: tuple[Leg, ...],
    underlying_price: Decimal,
    net_premium: Decimal,
    *,
    multiplier: int = 100,
    quantity: int = 1,
) -> Decimal:
    """Total dollar P&L of the whole position if expiry happened at this price."""
    require_money("underlying_price", underlying_price)
    if underlying_price < 0:
        raise DomainValidationError("underlying_price must be >= 0")
    require_money("net_premium", net_premium)
    require_positive_int("multiplier", multiplier)
    require_positive_int("quantity", quantity)
    if not legs:
        raise DomainValidationError("at least one leg required")
    return _pnl_per_share(legs, underlying_price, net_premium) * multiplier * quantity


def analyze(
    legs: tuple[Leg, ...],
    net_premium: Decimal,
    *,
    multiplier: int = 100,
    quantity: int = 1,
) -> PayoffProfile:
    """Compute max loss, max gain, and exact breakevens for a defined-risk structure."""
    require_money("net_premium", net_premium)
    require_positive_int("multiplier", multiplier)
    require_positive_int("quantity", quantity)
    if not legs:
        raise DomainValidationError("at least one leg required")

    # Slope of the per-share payoff for S above the highest strike: net signed call
    # quantity. Negative means loss grows without bound -> undefined risk -> reject.
    slope_right = Decimal("0")
    for leg in legs:
        if leg.option_type is OptionType.CALL:
            signed = leg.quantity if leg.side is LegSide.BUY else -leg.quantity
            slope_right += signed
    if slope_right < 0:
        raise DomainValidationError(
            "undefined risk: net short calls have unbounded loss (REQUIRE_DEFINED_MAX_LOSS)"
        )

    strikes = sorted({leg.strike for leg in legs})
    critical = [Decimal("0"), *strikes]
    scale = Decimal(multiplier) * Decimal(quantity)
    pnls = {s: _pnl_per_share(legs, s, net_premium) for s in critical}

    min_pnl = min(pnls.values())
    if min_pnl >= 0:
        raise DomainValidationError(
            "no losing expiry price exists; inputs are inconsistent with a real "
            "market (check net_premium sign and legs)"
        )
    max_loss = -min_pnl * scale

    unbounded_gain = slope_right > 0
    max_gain: Decimal | None = None
    if not unbounded_gain:
        peak = max(pnls.values())
        if peak <= 0:
            raise DomainValidationError(
                "structure can never profit at expiry; inputs are inconsistent "
                "(check net_premium sign and legs)"
            )
        max_gain = peak * scale

    # Exact breakevens: the payoff is piecewise linear between critical prices, so
    # each sign change pins a root by linear interpolation in Decimal.
    breakevens: list[Decimal] = []
    for s1, s2 in zip(critical, critical[1:], strict=False):
        p1, p2 = pnls[s1], pnls[s2]
        if p1 == 0:
            breakevens.append(s1)
        if (p1 < 0 < p2) or (p2 < 0 < p1):
            root = s1 + (-p1) * (s2 - s1) / (p2 - p1)
            breakevens.append(root.quantize(_FOUR_DP))
    last_strike = strikes[-1]
    p_last = pnls[last_strike]
    if p_last == 0 and last_strike not in breakevens:
        breakevens.append(last_strike)
    elif unbounded_gain and p_last < 0:
        root = last_strike + (-p_last) / slope_right
        breakevens.append(root.quantize(_FOUR_DP))

    if not breakevens:
        raise DomainValidationError("no breakeven found; inputs are inconsistent")

    return PayoffProfile(
        net_premium_per_share=net_premium,
        max_loss=max_loss,
        max_gain=max_gain,
        breakevens=tuple(sorted(set(breakevens))),
        unbounded_gain=unbounded_gain,
    )
