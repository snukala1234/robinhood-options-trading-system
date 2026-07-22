"""Technical feature service (spec 5.5): reproducible numbers, not chart opinions.

All prices are Decimal; all derived ratios are Decimal quantized to six decimal
places. Requires at least 50 bars (for SMA-50); shorter history is rejected, never
padded or extrapolated.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from src.domain.values import DomainValidationError, require_positive_money

_SIX_DP = Decimal("0.000001")

MIN_BARS = 50


@dataclass(frozen=True)
class Bar:
    """One daily OHLCV bar, validated."""

    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    def __post_init__(self) -> None:
        if not isinstance(self.day, date):
            raise DomainValidationError("day must be a date")
        for name in ("open", "high", "low", "close"):
            require_positive_money(name, getattr(self, name))
        if isinstance(self.volume, bool) or not isinstance(self.volume, int) or self.volume < 0:
            raise DomainValidationError(f"volume must be an int >= 0, got {self.volume!r}")
        if self.high < self.low:
            raise DomainValidationError(f"high {self.high} < low {self.low}")
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise DomainValidationError("open/close outside [low, high]")


@dataclass(frozen=True)
class TechnicalFeatures:
    """Deterministic features as of the last bar."""

    as_of: date
    close: Decimal
    sma_20: Decimal
    sma_50: Decimal
    distance_from_sma20_pct: Decimal
    roc_10: Decimal  # 10-bar rate of change, fraction
    atr_14: Decimal
    atr_pct: Decimal  # ATR / close
    volume_ratio_20: Decimal  # last volume / 20-bar mean volume
    trend: str  # "up" | "down" | "sideways"
    range_pct_20: Decimal  # (20-bar high - 20-bar low) / close
    recent_high_20: Decimal
    recent_low_20: Decimal


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, Decimal("0")) / len(values)


def compute_features(bars: Sequence[Bar]) -> TechnicalFeatures:
    """Compute features from chronological daily bars (oldest first)."""
    if len(bars) < MIN_BARS:
        raise DomainValidationError(f"need >= {MIN_BARS} bars, got {len(bars)}")
    days = [b.day for b in bars]
    if days != sorted(days) or len(set(days)) != len(days):
        raise DomainValidationError("bars must be chronological and unique by day")

    closes = [b.close for b in bars]
    last = bars[-1]

    sma_20 = _mean(closes[-20:])
    sma_50 = _mean(closes[-50:])
    distance = ((last.close - sma_20) / sma_20).quantize(_SIX_DP)
    roc_10 = (last.close / closes[-11] - 1).quantize(_SIX_DP)

    true_ranges: list[Decimal] = []
    for prev, cur in zip(bars[-15:-1], bars[-14:], strict=True):
        tr = max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        true_ranges.append(tr)
    atr_14 = _mean(true_ranges)
    atr_pct = (atr_14 / last.close).quantize(_SIX_DP)

    mean_volume = _mean([Decimal(b.volume) for b in bars[-20:]])
    if mean_volume == 0:
        raise DomainValidationError("20-bar mean volume is zero; data is unusable")
    volume_ratio = (Decimal(last.volume) / mean_volume).quantize(_SIX_DP)

    if last.close > sma_20 > sma_50:
        trend = "up"
    elif last.close < sma_20 < sma_50:
        trend = "down"
    else:
        trend = "sideways"

    recent_high = max(b.high for b in bars[-20:])
    recent_low = min(b.low for b in bars[-20:])
    range_pct = ((recent_high - recent_low) / last.close).quantize(_SIX_DP)

    return TechnicalFeatures(
        as_of=last.day,
        close=last.close,
        sma_20=sma_20.quantize(_SIX_DP),
        sma_50=sma_50.quantize(_SIX_DP),
        distance_from_sma20_pct=distance,
        roc_10=roc_10,
        atr_14=atr_14.quantize(_SIX_DP),
        atr_pct=atr_pct,
        volume_ratio_20=volume_ratio,
        trend=trend,
        range_pct_20=range_pct,
        recent_high_20=recent_high,
        recent_low_20=recent_low,
    )
