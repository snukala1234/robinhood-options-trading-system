"""Black-Scholes Greeks against hand-computed reference values + rejection tests.

Reference case: S=100, K=100, r=0.05, sigma=0.20, dte=73 (t=0.2 years exactly).
Hand computation:
    d1 = (ln(1) + (0.05 + 0.02) * 0.2) / (0.2 * sqrt(0.2)) = 0.156525
    d2 = d1 - 0.089443 = 0.067082
    N(d1) = 0.562192   phi(d1) = 0.394085   N(d2) = 0.526742
    delta_call = 0.562192
    gamma      = phi(d1) / (S sigma sqrt(t)) = 0.044060
    vega(1%)   = S phi(d1) sqrt(t) / 100    = 0.176243
    theta_call = [-S phi(d1) sigma / (2 sqrt(t)) - r K e^{-rt} N(d2)] / 365
               = -11.4195 / 365 = -0.031286 per day
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.analytics.greeks import GreekSet, GreekSource, black_scholes_greeks
from src.domain.instruments import OptionType
from src.domain.values import DomainValidationError

REF = {
    "spot": Decimal("100"),
    "strike": Decimal("100"),
    "dte": 73,
    "iv": Decimal("0.20"),
    "risk_free_rate": Decimal("0.05"),
}


def _close(actual: Decimal, expected: str, tol: str = "0.001") -> bool:
    return abs(actual - Decimal(expected)) <= Decimal(tol)


def test_call_greeks_match_hand_computed_reference() -> None:
    g = black_scholes_greeks(option_type=OptionType.CALL, **REF)  # type: ignore[arg-type]
    assert _close(g.delta, "0.562192")
    assert _close(g.gamma, "0.044060")
    assert _close(g.vega, "0.176243")
    assert _close(g.theta_daily, "-0.031286")
    assert g.source is GreekSource.CALCULATED
    assert g.assumptions is not None
    recorded = dict(g.assumptions)
    assert recorded["model"] == "black_scholes_european_no_dividends"
    assert recorded["iv"] == "0.20"
    assert recorded["day_count"] == "365"


def test_put_call_parity_relations() -> None:
    call = black_scholes_greeks(option_type=OptionType.CALL, **REF)  # type: ignore[arg-type]
    put = black_scholes_greeks(option_type=OptionType.PUT, **REF)  # type: ignore[arg-type]
    # Same d1: delta_put = delta_call - 1; gamma and vega identical.
    assert put.delta == call.delta - 1
    assert put.gamma == call.gamma
    assert put.vega == call.vega
    assert Decimal("-1") <= put.delta <= Decimal("0")


def test_deep_itm_call_delta_approaches_one() -> None:
    g = black_scholes_greeks(
        spot=Decimal("200"),
        strike=Decimal("100"),
        dte=7,
        iv=Decimal("0.20"),
        risk_free_rate=Decimal("0.05"),
        option_type=OptionType.CALL,
    )
    assert g.delta > Decimal("0.999")


def test_degenerate_inputs_are_rejected_not_extrapolated() -> None:
    good = dict(REF)
    for field, bad in [
        ("dte", 0),
        ("iv", Decimal("0")),
        ("iv", Decimal("-0.2")),
        ("iv", Decimal("10")),
        ("spot", Decimal("0")),
        ("strike", Decimal("-100")),
        ("risk_free_rate", Decimal("0.75")),
    ]:
        args = {**good, field: bad}
        with pytest.raises(DomainValidationError):
            black_scholes_greeks(option_type=OptionType.CALL, **args)  # type: ignore[arg-type]


def test_float_money_inputs_are_rejected() -> None:
    with pytest.raises(DomainValidationError):
        black_scholes_greeks(
            spot=100.0,  # type: ignore[arg-type]
            strike=Decimal("100"),
            dte=73,
            iv=Decimal("0.2"),
            risk_free_rate=Decimal("0.05"),
            option_type=OptionType.CALL,
        )


def test_calculated_greekset_requires_assumptions() -> None:
    with pytest.raises(DomainValidationError, match="assumptions"):
        GreekSet(
            delta=Decimal("0.5"),
            gamma=Decimal("0.04"),
            theta_daily=Decimal("-0.03"),
            vega=Decimal("0.18"),
            source=GreekSource.CALCULATED,
        )
    # Broker-supplied Greeks legitimately arrive without model assumptions.
    broker = GreekSet(
        delta=Decimal("0.5"),
        gamma=Decimal("0.04"),
        theta_daily=Decimal("-0.03"),
        vega=Decimal("0.18"),
        source=GreekSource.BROKER,
    )
    assert broker.source is GreekSource.BROKER


def test_greekset_bounds() -> None:
    with pytest.raises(DomainValidationError, match="delta"):
        GreekSet(
            delta=Decimal("1.5"),
            gamma=Decimal("0.04"),
            theta_daily=Decimal("-0.03"),
            vega=Decimal("0.18"),
            source=GreekSource.BROKER,
        )
    with pytest.raises(DomainValidationError, match="gamma"):
        GreekSet(
            delta=Decimal("0.5"),
            gamma=Decimal("-0.01"),
            theta_daily=Decimal("-0.03"),
            vega=Decimal("0.18"),
            source=GreekSource.BROKER,
        )
