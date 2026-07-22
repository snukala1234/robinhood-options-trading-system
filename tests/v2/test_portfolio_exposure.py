"""Portfolio Greeks, limit headroom, and delta-gamma stress — hand-verified."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from src.analytics.portfolio_exposure import (
    PositionExposure,
    aggregate,
    stress_scenarios,
)
from src.domain.values import DomainValidationError

D = Decimal


def _spy_spread() -> PositionExposure:
    """One SPY 600/605 debit spread: delta 0.24/unit, gamma 0.004, theta -6.20/day
    per unit... theta here is per-share daily (-0.062 x 100 = -$6.20/contract)."""
    return PositionExposure(
        position_id="pos-1",
        underlying="SPY",
        sector="index",
        strategy="bull_call_debit_spread",
        expiration=date(2026, 8, 7),
        spot=D("600"),
        net_delta_per_unit=D("0.24"),
        net_gamma_per_unit=D("0.004"),
        net_theta_daily_per_unit=D("-0.062"),
        net_vega_per_unit=D("0.081"),
        quantity=1,
        multiplier=100,
        max_loss=D("185.00"),
    )


def test_dollar_greeks_hand_verified() -> None:
    p = _spy_spread()
    # delta$ = 0.24 x 600 x 100 = 14,400
    assert p.delta_dollars == D("14400.00")
    # gamma$ = 0.5 x 0.004 x 600^2 x 100 = 72,000
    assert p.gamma_dollars == D("72000.000")
    # theta$/day = -0.062 x 100 = -6.20
    assert p.theta_dollars_daily == D("-6.200")
    # vega$/pct = 0.081 x 100 = 8.10
    assert p.vega_dollars_per_pct == D("8.100")


def test_aggregate_sums_and_buckets() -> None:
    second = PositionExposure(
        position_id="pos-2",
        underlying="QQQ",
        sector="index",
        strategy="long_put",
        expiration=date(2026, 8, 14),
        spot=D("500"),
        net_delta_per_unit=D("-0.30"),
        net_gamma_per_unit=D("0.005"),
        net_theta_daily_per_unit=D("-0.050"),
        net_vega_per_unit=D("0.070"),
        quantity=1,
        multiplier=100,
        max_loss=D("150.00"),
    )
    exp = aggregate([_spy_spread(), second], account_equity=D("100000"))
    # net delta$ = 14,400 + (-0.30 x 500 x 100 = -15,000) = -600
    assert exp.net_delta_dollars == D("-600.00")
    assert exp.gross_delta_dollars == D("29400.00")
    assert exp.theta_dollars_daily == D("-11.200")
    assert exp.open_risk == D("335.00")
    assert exp.risk_by_underlying == {"SPY": D("185.00"), "QQQ": D("150.00")}
    assert exp.risk_by_sector == {"index": D("335.00")}
    assert exp.risk_by_expiration[date(2026, 8, 7)] == D("185.00")


def test_limit_checks_flag_breaches() -> None:
    """With $1,000 equity the single SPY spread breaches theta burn (6.20/1000 =
    0.0062 > 0.004), open risk (0.185 > 0.05), and underlying risk (0.185 > 0.015)."""
    exp = aggregate([_spy_spread()], account_equity=D("1000"))
    breached = {c.name for c in exp.breached_limits()}
    assert "daily_theta_burn_pct" in breached
    assert "total_open_risk_pct" in breached
    assert "underlying_risk_pct:SPY" in breached
    # With $100k equity the same position breaches nothing.
    exp_big = aggregate([_spy_spread()], account_equity=D("100000"))
    assert exp_big.breached_limits() == ()


def test_stress_scenarios_hand_verified() -> None:
    """+2%: 14,400 x 0.02 + 72,000 x 0.0004 = 288 + 28.80 = 316.80.
    -10%: 14,400 x -0.10 + 72,000 x 0.01 = -1,440 + 720 = -720, floored at -185."""
    scenarios = {s.move: s for s in stress_scenarios([_spy_spread()], [D("0.02"), D("-0.10")])}
    up = scenarios[D("0.02")]
    assert up.estimated_pnl == D("316.80")
    assert up.bounded_by_max_loss == D("316.80")
    down = scenarios[D("-0.10")]
    assert down.estimated_pnl == D("-720.00")
    assert down.bounded_by_max_loss == D("-185.00")


def test_invalid_inputs_rejected() -> None:
    with pytest.raises(DomainValidationError):
        aggregate([_spy_spread()], account_equity=D("0"))
    with pytest.raises(DomainValidationError, match="stress move"):
        stress_scenarios([_spy_spread()], [D("2")])
    with pytest.raises(DomainValidationError, match="at least one"):
        stress_scenarios([_spy_spread()], [])
    with pytest.raises(DomainValidationError):
        PositionExposure(
            position_id="bad",
            underlying="SPY",
            sector="index",
            strategy="long_call",
            expiration=date(2026, 8, 7),
            spot=D("600"),
            net_delta_per_unit=D("0.24"),
            net_gamma_per_unit=D("0.004"),
            net_theta_daily_per_unit=D("-0.062"),
            net_vega_per_unit=D("0.081"),
            quantity=1,
            multiplier=100,
            max_loss=D("0"),  # a position with no defined loss cannot exist
        )
