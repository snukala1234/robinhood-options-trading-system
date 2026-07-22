"""Volatility service: hand-verified values and fail-closed input handling."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from src.analytics.volatility import (
    TermPoint,
    expected_move,
    iv_realized_spread,
    put_call_skew,
    realized_volatility,
    term_structure,
)
from src.domain.values import DomainValidationError

D = Decimal


def test_constant_prices_have_zero_realized_volatility() -> None:
    closes = [D("100")] * 12
    assert realized_volatility(closes) == D("0.000000")


def test_alternating_returns_hand_verified() -> None:
    """Log returns alternate +1%/-1% (12 returns): mean 0, sample stdev
    = sqrt(12 * 0.0001 / 11) = 0.0104447; annualized x sqrt(252) = 0.165804."""
    closes = [D("100")]
    for i in range(12):
        step = D("0.01") if i % 2 == 0 else D("-0.01")
        closes.append(closes[-1] * step.exp())
    rv = realized_volatility(closes)
    assert abs(rv - D("0.165804")) < D("0.0005")


def test_too_few_observations_rejected() -> None:
    with pytest.raises(DomainValidationError, match="need >="):
        realized_volatility([D("100")] * 10)  # 9 returns < 10 minimum


def test_non_positive_price_rejected() -> None:
    closes = [D("100")] * 11 + [D("0")]
    with pytest.raises(DomainValidationError):
        realized_volatility(closes)


def test_expected_move_hand_verified() -> None:
    """600 x 0.20 x sqrt(14/365) = 600 x 0.20 x 0.195846 = 23.50."""
    assert expected_move(D("600"), D("0.20"), 14) == D("23.50")


def test_expected_move_rejects_degenerate_inputs() -> None:
    with pytest.raises(DomainValidationError):
        expected_move(D("600"), D("0.20"), 0)
    with pytest.raises(DomainValidationError):
        expected_move(D("600"), D("0"), 14)
    with pytest.raises(DomainValidationError):
        expected_move(600.0, D("0.20"), 14)  # type: ignore[arg-type]


def test_term_structure_classification() -> None:
    contango = term_structure(
        [
            TermPoint(date(2026, 7, 28), 7, D("0.22")),
            TermPoint(date(2026, 8, 20), 30, D("0.27")),
        ]
    )
    assert contango.shape == "contango"
    assert contango.slope == D("0.05")
    assert contango.near_iv == D("0.22") and contango.far_iv == D("0.27")

    backwardation = term_structure(
        [
            TermPoint(date(2026, 8, 20), 30, D("0.25")),
            TermPoint(date(2026, 7, 28), 7, D("0.30")),  # unsorted input is fine
        ]
    )
    assert backwardation.shape == "backwardation"
    assert backwardation.slope == D("-0.05")

    flat = term_structure(
        [
            TermPoint(date(2026, 7, 28), 7, D("0.250")),
            TermPoint(date(2026, 8, 20), 30, D("0.253")),
        ]
    )
    assert flat.shape == "flat"


def test_term_structure_rejects_bad_inputs() -> None:
    with pytest.raises(DomainValidationError, match=">= 2"):
        term_structure([TermPoint(date(2026, 7, 28), 7, D("0.25"))])
    with pytest.raises(DomainValidationError, match="duplicate"):
        term_structure(
            [
                TermPoint(date(2026, 7, 28), 7, D("0.25")),
                TermPoint(date(2026, 7, 28), 7, D("0.26")),
            ]
        )
    with pytest.raises(DomainValidationError):
        TermPoint(date(2026, 7, 28), 7, D("0"))  # zero IV rejected


def test_skew_and_iv_spread() -> None:
    assert put_call_skew(D("0.32"), D("0.28")) == D("0.04")
    assert iv_realized_spread(D("0.25"), D("0.18")) == D("0.07")
    with pytest.raises(DomainValidationError):
        put_call_skew(D("0"), D("0.28"))
    with pytest.raises(DomainValidationError):
        iv_realized_spread(D("0.25"), D("-0.1"))
