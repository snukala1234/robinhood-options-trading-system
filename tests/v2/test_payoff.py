"""Payoff/breakeven/max-loss with hand-verified numbers for every strategy shape."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.analytics.payoff import analyze, payoff_at_expiry
from src.domain.instruments import Leg, LegSide, OptionType
from src.domain.values import DomainValidationError

D = Decimal

BULL_CALL_600_605 = (
    Leg(LegSide.BUY, OptionType.CALL, D("600"), 1),
    Leg(LegSide.SELL, OptionType.CALL, D("605"), 1),
)


def test_bull_call_debit_spread_hand_verified() -> None:
    """The spec's own example: 600/605 call spread for 1.85 debit.

    Max loss = 1.85 x 100 = $185; max gain = (5.00 - 1.85) x 100 = $315;
    breakeven = 600 + 1.85 = 601.85.
    """
    p = analyze(BULL_CALL_600_605, D("1.85"))
    assert p.max_loss == D("185.00")
    assert p.max_gain == D("315.00")
    assert p.breakevens == (D("601.85"),)
    assert not p.unbounded_gain


def test_long_call_hand_verified() -> None:
    """Long 600 call for 2.50: max loss $250, unbounded gain, breakeven 602.50."""
    legs = (Leg(LegSide.BUY, OptionType.CALL, D("600"), 1),)
    p = analyze(legs, D("2.50"))
    assert p.max_loss == D("250.00")
    assert p.max_gain is None
    assert p.unbounded_gain
    assert p.breakevens == (D("602.5000"),)


def test_long_put_hand_verified() -> None:
    """Long 600 put for 2.50: max loss $250, max gain (600-2.50)x100, BE 597.50."""
    legs = (Leg(LegSide.BUY, OptionType.PUT, D("600"), 1),)
    p = analyze(legs, D("2.50"))
    assert p.max_loss == D("250.00")
    assert p.max_gain == D("59750.00")
    assert p.breakevens == (D("597.5000"),)


def test_bear_put_debit_spread_hand_verified() -> None:
    """Buy 605 put / sell 600 put for 1.90: max loss $190, max gain $310, BE 603.10."""
    legs = (
        Leg(LegSide.BUY, OptionType.PUT, D("605"), 1),
        Leg(LegSide.SELL, OptionType.PUT, D("600"), 1),
    )
    p = analyze(legs, D("1.90"))
    assert p.max_loss == D("190.00")
    assert p.max_gain == D("310.00")
    assert p.breakevens == (D("603.1000"),)


def test_put_credit_spread_hand_verified() -> None:
    """Sell 595 put / buy 590 put for 1.20 credit: max gain $120, max loss $380,
    breakeven 595 - 1.20 = 593.80. Credit = negative net premium."""
    legs = (
        Leg(LegSide.BUY, OptionType.PUT, D("590"), 1),
        Leg(LegSide.SELL, OptionType.PUT, D("595"), 1),
    )
    p = analyze(legs, D("-1.20"))
    assert p.max_loss == D("380.00")
    assert p.max_gain == D("120.00")
    assert p.breakevens == (D("593.8000"),)


def test_quantity_scales_dollar_outcomes_not_breakevens() -> None:
    p = analyze(BULL_CALL_600_605, D("1.85"), quantity=3)
    assert p.max_loss == D("555.00")
    assert p.max_gain == D("945.00")
    assert p.breakevens == (D("601.85"),)


def test_payoff_at_expiry_point_values() -> None:
    args = (BULL_CALL_600_605, D("1.85"))
    assert payoff_at_expiry(args[0], D("600"), args[1]) == D("-185.00")
    assert payoff_at_expiry(args[0], D("610"), args[1]) == D("315.00")
    # At 603: intrinsic 3.00 - 1.85 = 1.15 per share -> $115.
    assert payoff_at_expiry(args[0], D("603"), args[1]) == D("115.00")
    assert payoff_at_expiry(args[0], D("603"), args[1], quantity=2) == D("230.00")


def test_naked_short_call_rejected_as_undefined_risk() -> None:
    legs = (Leg(LegSide.SELL, OptionType.CALL, D("600"), 1),)
    with pytest.raises(DomainValidationError, match="undefined risk"):
        analyze(legs, D("-2.50"))


def test_call_credit_spread_is_defined_and_hand_verified() -> None:
    """Sell 600 call / buy 605 call for 1.85 credit: max gain $185, max loss $315."""
    legs = (
        Leg(LegSide.SELL, OptionType.CALL, D("600"), 1),
        Leg(LegSide.BUY, OptionType.CALL, D("605"), 1),
    )
    p = analyze(legs, D("-1.85"))
    assert p.max_loss == D("315.00")
    assert p.max_gain == D("185.00")
    assert p.breakevens == (D("601.8500"),)


def test_inconsistent_inputs_rejected_not_analyzed() -> None:
    # A long call "for a credit" would have no losing price -> inputs are wrong.
    legs = (Leg(LegSide.BUY, OptionType.CALL, D("600"), 1),)
    with pytest.raises(DomainValidationError, match="inconsistent"):
        analyze(legs, D("-2.50"))
    with pytest.raises(DomainValidationError, match="at least one leg"):
        analyze((), D("1.85"))
    with pytest.raises(DomainValidationError):
        analyze(legs, 2.50)  # type: ignore[arg-type]  # float money rejected
