"""Market-data ingestion (Section 6 step 1): OHLCV, ATR-14, volume-vs-average.

Primary source is yfinance (sufficient for Phase 1). Because the build must run
green hermetically (no network, no credentials), every fetch falls back to a
*deterministic* offline fixture seeded by the symbol, so a paper run is fully
reproducible and never depends on a live quote.

Mode is controlled by the ``TRADING_MARKET_DATA`` env var:
    "auto"    (default) try yfinance, fall back to offline on any failure
    "offline" always use the deterministic fixture (used by tests and run_paper)
    "online"  require yfinance; raise if it fails
"""

from __future__ import annotations

import math
import os
import random
from collections.abc import Callable
from typing import TYPE_CHECKING

from core.logging_setup import get_logger
from core.records import MarketSnapshot
from core.util import utcnow_iso

if TYPE_CHECKING:
    import pandas as pd

_log = get_logger("market_data")

# ---------------------------------------------------------------------------
# Point-in-time data seam (used ONLY by the backtest harness).
#
# When a provider is installed, get_snapshot() delegates to it. This lets the
# backtest inject as-of-date snapshots computed from bars up to and including the
# simulated day, so the real pipeline sees no future data. It changes no agent,
# guardrail, sizing, or exit logic and is a no-op when unset (live/paper paths).
# ---------------------------------------------------------------------------
_SNAPSHOT_PROVIDER: Callable[[str], MarketSnapshot | None] | None = None


def set_snapshot_provider(provider: Callable[[str], MarketSnapshot | None]) -> None:
    """Install a point-in-time snapshot provider (backtest only)."""
    global _SNAPSHOT_PROVIDER
    _SNAPSHOT_PROVIDER = provider


def clear_snapshot_provider() -> None:
    """Remove any installed snapshot provider (restore live/offline behaviour)."""
    global _SNAPSHOT_PROVIDER
    _SNAPSHOT_PROVIDER = None

# Market regime labels (kept intentionally coarse; Agent 8 tracks regime drift).
REGIME_CALM = "calm"
REGIME_NORMAL = "normal"
REGIME_VOLATILE = "volatile"


def mode() -> str:
    return os.environ.get("TRADING_MARKET_DATA", "auto").lower()


# ---------------------------------------------------------------------------
# Online path (yfinance)
# ---------------------------------------------------------------------------

def _atr_from_frame(frame: pd.DataFrame, period: int = 14) -> float:
    """Compute ATR over the last ``period`` bars using the standard true-range method."""
    highs = frame["High"].astype(float)
    lows = frame["Low"].astype(float)
    closes = frame["Close"].astype(float)
    prev_close = closes.shift(1)

    tr = (highs - lows).abs()
    tr2 = (highs - prev_close).abs()
    tr3 = (lows - prev_close).abs()
    true_range = tr.combine(tr2, max).combine(tr3, max)
    atr = true_range.tail(period).mean()
    return float(atr)


def _fetch_online(symbol: str) -> MarketSnapshot:
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    frame = ticker.history(period="3mo", interval="1d")
    if frame is None or frame.empty or len(frame) < 15:
        raise RuntimeError(f"insufficient bars for {symbol}")

    closes = frame["Close"].astype(float)
    volumes = frame["Volume"].astype(float)
    current_price = float(closes.iloc[-1])
    atr_14 = _atr_from_frame(frame, 14)
    atr_pct = atr_14 / current_price if current_price else 0.0
    volume = float(volumes.iloc[-1])
    avg_volume = float(volumes.tail(20).mean())
    volume_ratio = volume / avg_volume if avg_volume else 1.0

    return MarketSnapshot(
        symbol=symbol,
        current_price=round(current_price, 4),
        atr_14=round(atr_14, 4),
        atr_pct=round(atr_pct, 6),
        volume=volume,
        avg_volume=avg_volume,
        volume_ratio=round(volume_ratio, 4),
        as_of=utcnow_iso(),
    )


# ---------------------------------------------------------------------------
# Offline deterministic fixture
# ---------------------------------------------------------------------------

def _seed_for(symbol: str) -> int:
    # Stable, platform-independent seed from the symbol.
    return sum((i + 1) * ord(c) for i, c in enumerate(symbol.upper()))


def _offline_snapshot(symbol: str) -> MarketSnapshot:
    """Deterministic plausible snapshot so paper runs are reproducible offline."""
    rng = random.Random(_seed_for(symbol))
    # Base price in a realistic band; ATR% between ~1% and ~4%.
    current_price = round(rng.uniform(20.0, 400.0), 2)
    atr_pct = round(rng.uniform(0.01, 0.04), 6)
    atr_14 = round(current_price * atr_pct, 4)
    avg_volume = float(rng.randint(2_000_000, 60_000_000))
    volume_ratio = round(rng.uniform(0.6, 1.8), 4)
    volume = round(avg_volume * volume_ratio, 0)
    return MarketSnapshot(
        symbol=symbol,
        current_price=current_price,
        atr_14=atr_14,
        atr_pct=atr_pct,
        volume=volume,
        avg_volume=avg_volume,
        volume_ratio=volume_ratio,
        as_of=utcnow_iso(),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_snapshot(symbol: str) -> MarketSnapshot:
    """Return a market snapshot for one symbol, honouring the configured mode."""
    if _SNAPSHOT_PROVIDER is not None:
        snap = _SNAPSHOT_PROVIDER(symbol)
        if snap is not None:
            return snap
    m = mode()
    if m == "offline":
        return _offline_snapshot(symbol)
    try:
        return _fetch_online(symbol)
    except Exception as exc:  # noqa: BLE001 - offline fallback is the whole point
        if m == "online":
            raise
        _log.warning("online fetch failed for %s (%s); using offline fixture", symbol, exc)
        return _offline_snapshot(symbol)


def get_snapshots(symbols: list[str]) -> dict[str, MarketSnapshot]:
    """Return snapshots for many symbols keyed by symbol."""
    return {s: get_snapshot(s) for s in symbols}


def get_market_regime(proxy_symbol: str = "SPY") -> str:
    """Classify the broad market regime from a proxy's ATR% (Section 3.4).

    This is condition-adaptation input, deliberately independent of trade outcomes.
    """
    snap = get_snapshot(proxy_symbol)
    if snap.atr_pct >= 0.03:
        return REGIME_VOLATILE
    if snap.atr_pct <= 0.012:
        return REGIME_CALM
    return REGIME_NORMAL


def price_move_fraction(entry_price: float, current_price: float) -> float:
    """Signed fractional move from entry to current (positive = up). Guards div-by-zero."""
    if entry_price == 0:
        return 0.0
    return (current_price - entry_price) / entry_price


def is_finite_positive(x: float) -> bool:
    return math.isfinite(x) and x > 0
