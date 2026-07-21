"""Historical daily-bar store for the backtest (real data via yfinance, cached).

The store is the ONLY data source the backtest injects into the pipeline. Its central
guarantee is **no look-ahead**: :meth:`snapshot_asof` computes a ``MarketSnapshot`` from bars
up to and including the as-of date only, and :meth:`bar_on` returns exactly one day's OHLC.
Bars are cached to disk so a multi-year backtest is fast and reproducible after the first run.
"""

from __future__ import annotations

import hashlib
import pickle
from dataclasses import dataclass

import pandas as pd

from config import settings
from core.logging_setup import get_logger
from core.records import MarketSnapshot

_log = get_logger("backtest.data")

CACHE_DIR = settings.REPO_ROOT / "data" / "backtest_cache"


@dataclass
class DayBar:
    open: float
    high: float
    low: float
    close: float
    volume: float


def _atr(frame: pd.DataFrame, period: int = 14) -> float:
    """Standard true-range ATR over the last ``period`` bars (matches core.market_data)."""
    highs = frame["High"].astype(float)
    lows = frame["Low"].astype(float)
    closes = frame["Close"].astype(float)
    prev_close = closes.shift(1)
    tr = (highs - lows).abs()
    tr2 = (highs - prev_close).abs()
    tr3 = (lows - prev_close).abs()
    true_range = tr.combine(tr2, max).combine(tr3, max)
    return float(true_range.tail(period).mean())


class HistoricalBarStore:
    """Point-in-time OHLCV store for a basket of symbols."""

    def __init__(self, bars: dict[str, pd.DataFrame], calendar_symbol: str = "SPY") -> None:
        self.bars = bars
        self.calendar_symbol = calendar_symbol if calendar_symbol in bars else next(iter(bars))
        self._current_date: pd.Timestamp | None = None

    # -- construction ------------------------------------------------------

    @classmethod
    def fetch(
        cls,
        symbols: list[str],
        start: str,
        end: str,
        calendar_symbol: str = "SPY",
        force_refresh: bool = False,
    ) -> HistoricalBarStore:
        """Fetch (or load cached) daily bars for ``symbols`` over [start, end]."""
        all_symbols = sorted(set(symbols) | {calendar_symbol})
        key = hashlib.sha1(f"{all_symbols}|{start}|{end}".encode()).hexdigest()[:16]
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache = CACHE_DIR / f"bars_{key}.pkl"

        if cache.exists() and not force_refresh:
            with cache.open("rb") as fh:
                cached = pickle.load(fh)  # noqa: S301 - our own cache file
            _log.info("loaded %d symbols from cache %s", len(cached), cache.name)
            return cls(cached, calendar_symbol)

        import yfinance as yf

        bars: dict[str, pd.DataFrame] = {}
        for sym in all_symbols:
            try:
                df = yf.Ticker(sym).history(start=start, end=end, interval="1d", auto_adjust=True)
            except Exception as exc:  # noqa: BLE001
                _log.warning("fetch failed for %s: %s", sym, exc)
                continue
            if df is None or df.empty:
                _log.warning("no data for %s", sym)
                continue
            df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
            df.index = pd.DatetimeIndex(df.index).tz_localize(None).normalize()
            df = df[~df.index.duplicated(keep="last")].sort_index()
            bars[sym] = df
        if calendar_symbol not in bars:
            raise RuntimeError(f"calendar symbol {calendar_symbol} unavailable; cannot backtest")
        with cache.open("wb") as fh:
            pickle.dump(bars, fh)
        _log.info("fetched %d symbols, cached to %s", len(bars), cache.name)
        return cls(bars, calendar_symbol)

    # -- calendar / clock --------------------------------------------------

    def trading_dates(self, warmup: int = 25) -> list[pd.Timestamp]:
        """Dates to simulate, driven by the calendar symbol, after a warmup window."""
        idx = list(self.bars[self.calendar_symbol].index)
        return idx[warmup:]

    def set_current_date(self, date: pd.Timestamp) -> None:
        self._current_date = date

    @property
    def current_date(self) -> pd.Timestamp | None:
        return self._current_date

    def symbols(self) -> list[str]:
        return [s for s in self.bars if s != self.calendar_symbol]

    # -- point-in-time reads (NO look-ahead) -------------------------------

    def snapshot_asof(self, symbol: str, date: pd.Timestamp) -> MarketSnapshot | None:
        """Snapshot from bars up to AND INCLUDING ``date`` only. None if insufficient history."""
        df = self.bars.get(symbol)
        if df is None:
            return None
        sub = df.loc[:date]
        if len(sub) < 15:
            return None
        close = float(sub["Close"].iloc[-1])
        atr = _atr(sub, 14)
        atr_pct = atr / close if close else 0.0
        volume = float(sub["Volume"].iloc[-1])
        avg_volume = float(sub["Volume"].tail(20).mean())
        volume_ratio = volume / avg_volume if avg_volume else 1.0
        return MarketSnapshot(
            symbol=symbol,
            current_price=round(close, 4),
            atr_14=round(atr, 4),
            atr_pct=round(atr_pct, 6),
            volume=volume,
            avg_volume=avg_volume,
            volume_ratio=round(volume_ratio, 4),
            as_of=pd.Timestamp(date).date().isoformat(),
        )

    def provider(self, symbol: str) -> MarketSnapshot | None:
        """Snapshot provider bound to the current simulated date (for market_data hook)."""
        if self._current_date is None:
            return None
        return self.snapshot_asof(symbol, self._current_date)

    def bar_on(self, symbol: str, date: pd.Timestamp) -> DayBar | None:
        """Exactly that day's OHLC (used for marking + stop/TP checks). None if no bar."""
        df = self.bars.get(symbol)
        if df is None or date not in df.index:
            return None
        row = df.loc[date]
        return DayBar(
            open=float(row["Open"]), high=float(row["High"]), low=float(row["Low"]),
            close=float(row["Close"]), volume=float(row["Volume"]),
        )

    def close_on(self, symbol: str, date: pd.Timestamp) -> float | None:
        bar = self.bar_on(symbol, date)
        return bar.close if bar else None
