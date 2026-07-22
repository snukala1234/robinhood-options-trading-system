"""Chain normalization: provenance, freshness, and rejection of malformed input."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from src.analytics.greeks import GreekSource
from src.data.option_chains import (
    StaleQuoteError,
    normalize_chain,
    normalize_contract,
)
from src.domain.values import DomainValidationError

D = Decimal
NOW = datetime(2026, 7, 21, 15, 30, 0, tzinfo=UTC)


def _raw(**overrides: Any) -> dict[str, Any]:
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
    return record


def test_normalization_produces_exact_decimals() -> None:
    q = normalize_contract(_raw(), source="test-feed", received_at=NOW)
    assert q.bid == D("1.80") and q.ask == D("1.90")
    assert q.midpoint == D("1.85")
    assert q.contract.occ_symbol() == "SPY260807C00600000"
    assert q.source == "test-feed"
    assert q.greeks is None  # absent means absent, never fabricated


def test_missing_required_field_rejected() -> None:
    for missing in ("bid", "ask", "observed_at", "strike", "open_interest"):
        raw = _raw()
        del raw[missing]
        with pytest.raises(DomainValidationError, match=missing):
            normalize_contract(raw, source="t", received_at=NOW)


def test_crossed_market_rejected() -> None:
    with pytest.raises(DomainValidationError, match="crossed"):
        normalize_contract(_raw(bid="2.00", ask="1.90"), source="t", received_at=NOW)


def test_float_money_rejected_with_explanation() -> None:
    with pytest.raises(DomainValidationError, match="float"):
        normalize_contract(_raw(strike=600.0), source="t", received_at=NOW)
    with pytest.raises(DomainValidationError, match="float"):
        normalize_contract(_raw(bid=1.8), source="t", received_at=NOW)


def test_partial_broker_greeks_rejected_never_padded() -> None:
    with pytest.raises(DomainValidationError, match="incomplete"):
        normalize_contract(
            _raw(greeks={"delta": "0.24", "gamma": "0.04"}),
            source="t",
            received_at=NOW,
        )


def test_full_broker_greeks_labeled_broker() -> None:
    q = normalize_contract(
        _raw(
            greeks={
                "delta": "0.24",
                "gamma": "0.04",
                "theta_daily": "-0.062",
                "vega": "0.081",
            }
        ),
        source="t",
        received_at=NOW,
    )
    assert q.greeks is not None
    assert q.greeks.source is GreekSource.BROKER
    assert q.greeks.delta == D("0.24")


def test_stale_quote_raises_instead_of_serving() -> None:
    q = normalize_contract(_raw(), source="t", received_at=NOW)
    q.require_fresh(NOW + timedelta(seconds=3))  # within the 5s limit: fine
    with pytest.raises(StaleQuoteError, match="old"):
        q.require_fresh(NOW + timedelta(seconds=10))


def test_future_timestamp_is_clock_skew_not_freshness() -> None:
    q = normalize_contract(_raw(), source="t", received_at=NOW)
    with pytest.raises(StaleQuoteError, match="future"):
        q.require_fresh(NOW - timedelta(seconds=2))


def test_implausible_iv_rejected() -> None:
    with pytest.raises(DomainValidationError, match="implausible"):
        normalize_contract(_raw(implied_volatility="12"), source="t", received_at=NOW)


def test_chain_sorted_and_duplicates_rejected() -> None:
    chain = normalize_chain(
        [
            _raw(strike="605"),
            _raw(strike="600", option_type="put"),
            _raw(strike="600"),
        ],
        source="t",
        received_at=NOW,
    )
    keys = [(q.contract.strike, q.contract.option_type.value) for q in chain]
    assert keys == [(D("600"), "call"), (D("600"), "put"), (D("605"), "call")]

    with pytest.raises(DomainValidationError, match="duplicate"):
        normalize_chain([_raw(), _raw()], source="t", received_at=NOW)
