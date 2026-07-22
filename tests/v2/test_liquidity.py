"""Liquidity assessment: hand-verified costs and policy-floor failures."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from src.analytics.liquidity import assess
from src.data.option_chains import normalize_contract
from src.domain.values import DomainValidationError

D = Decimal
NOW = datetime(2026, 7, 21, 15, 30, 0, tzinfo=UTC)


def _quote(**overrides: Any) -> Any:
    record: dict[str, Any] = {
        "underlying": "SPY",
        "expiration": "2026-08-07",
        "strike": "600",
        "option_type": "call",
        "bid": "1.80",
        "ask": "1.90",
        "volume": 250,
        "open_interest": 1200,
        "observed_at": NOW.isoformat(),
    }
    record.update(overrides)
    return normalize_contract(record, source="t", received_at=NOW)


def test_liquid_contract_hand_verified_costs() -> None:
    """Spread 0.10 on mid 1.85: pct 0.054054; for 2 contracts x100 the half-spread
    entry slippage is 0.05 x 200 = $10 and the round trip is 0.10 x 200 = $20."""
    a = assess(_quote(), quantity=2)
    assert a.spread_abs == D("0.10")
    assert a.midpoint == D("1.85")
    assert a.spread_pct == D("0.054054")
    assert a.est_entry_slippage == D("10.00")
    assert a.est_round_trip_cost == D("20.00")
    assert a.passes and a.failures == ()


def test_low_open_interest_fails_floor() -> None:
    a = assess(_quote(open_interest=50), quantity=1)
    assert not a.passes
    assert any("open_interest" in f for f in a.failures)


def test_low_volume_fails_floor() -> None:
    a = assess(_quote(volume=5), quantity=1)
    assert not a.passes
    assert any("volume" in f for f in a.failures)


def test_wide_spread_fails_floor() -> None:
    """bid 1.00 / ask 1.30: mid 1.15, spread pct 0.260870 > 0.12 limit."""
    a = assess(_quote(bid="1.00", ask="1.30"), quantity=1)
    assert a.spread_pct == D("0.260870")
    assert not a.passes
    assert any("spread_pct" in f for f in a.failures)


def test_sub_minimum_contract_price_fails_floor() -> None:
    a = assess(_quote(bid="0.04", ask="0.06"), quantity=1)
    assert a.midpoint == D("0.05")
    assert not a.passes
    assert any("midpoint" in f for f in a.failures)


def test_multiple_failures_all_reported() -> None:
    a = assess(_quote(bid="0.04", ask="0.06", volume=1, open_interest=2), quantity=1)
    assert len(a.failures) >= 3


def test_invalid_quantity_rejected() -> None:
    with pytest.raises(DomainValidationError):
        assess(_quote(), quantity=0)
