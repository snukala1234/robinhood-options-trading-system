"""Technical features on synthetic series with exactly computable values."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.analytics.technicals import Bar, compute_features
from src.domain.values import DomainValidationError

D = Decimal
START = date(2026, 1, 5)


def _ramp_bars(n: int = 60) -> list[Bar]:
    """Closes 1..n, high = close+1, low = close-1, volume 1000."""
    bars = []
    for i in range(1, n + 1):
        c = D(i)
        bars.append(
            Bar(
                day=START + timedelta(days=i),
                open=c,
                high=c + 1,
                low=max(c - 1, D("0.5")),
                close=c,
                volume=1000,
            )
        )
    return bars


def test_ramp_features_hand_verified() -> None:
    """Closes 1..60: SMA20 of 41..60 = 50.5; SMA50 of 11..60 = 35.5; ROC10 =
    60/50 - 1 = 0.2; ATR14 = 2 (TR is exactly 2 every bar); trend up."""
    f = compute_features(_ramp_bars())
    assert f.close == D("60")
    assert f.sma_20 == D("50.500000")
    assert f.sma_50 == D("35.500000")
    assert f.roc_10 == D("0.200000")
    assert f.atr_14 == D("2.000000")
    # distance = (60 - 50.5) / 50.5 = 0.188119 (6 dp)
    assert f.distance_from_sma20_pct == D("0.188119")
    # atr_pct = 2/60 = 0.033333
    assert f.atr_pct == D("0.033333")
    assert f.volume_ratio_20 == D("1.000000")
    assert f.trend == "up"
    assert f.recent_high_20 == D("61")
    assert f.recent_low_20 == D("40")
    # range = (61 - 40) / 60 = 0.35
    assert f.range_pct_20 == D("0.350000")


def test_downtrend_classification() -> None:
    """Closes 100 down to 41: close 41 < SMA20 (50.5) < SMA50 (65.5)."""
    bars = []
    for i, c in enumerate(range(100, 40, -1)):
        cc = D(c)
        bars.append(
            Bar(
                day=START + timedelta(days=i),
                open=cc,
                high=cc + 1,
                low=cc - 1,
                close=cc,
                volume=1000,
            )
        )
    f = compute_features(bars)
    assert f.sma_20 == D("50.500000")
    assert f.sma_50 == D("65.500000")
    assert f.trend == "down"


def test_short_history_rejected_not_padded() -> None:
    with pytest.raises(DomainValidationError, match="need >= 50"):
        compute_features(_ramp_bars(49))


def test_unsorted_or_duplicate_days_rejected() -> None:
    bars = _ramp_bars()
    bars[10], bars[11] = bars[11], bars[10]
    with pytest.raises(DomainValidationError, match="chronological"):
        compute_features(bars)


def test_zero_volume_series_rejected() -> None:
    bars = [
        Bar(
            day=b.day,
            open=b.open,
            high=b.high,
            low=b.low,
            close=b.close,
            volume=0,
        )
        for b in _ramp_bars()
    ]
    with pytest.raises(DomainValidationError, match="volume"):
        compute_features(bars)


def test_malformed_bar_rejected() -> None:
    with pytest.raises(DomainValidationError, match="high"):
        Bar(day=START, open=D("10"), high=D("9"), low=D("10"), close=D("10"), volume=1)
    with pytest.raises(DomainValidationError):
        Bar(day=START, open=D("12"), high=D("11"), low=D("9"), close=D("10"), volume=1)
    with pytest.raises(DomainValidationError):
        Bar(day=START, open=10.0, high=D("11"), low=D("9"), close=D("10"), volume=1)  # type: ignore[arg-type]
