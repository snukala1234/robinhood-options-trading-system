"""Point-in-time fundamentals for the backtest (real yfinance quarterly revenue, cached).

Look-ahead-safe: a quarter's revenue only becomes usable on ``period_end + REPORTING_LAG_DAYS``
(a conservative filing lag), never before. ``growth_asof`` returns YoY revenue growth when 5+
quarters are available, else QoQ (seasonally noisier), else None (agent stays neutral).

Data limitation (honest): yfinance exposes only ~5 recent quarters, so with the lag applied the
YoY comparison is only computable near the END of a short window; earlier dates fall back to QoQ
or None. Real fundamental signal is therefore sparse over a 6-month slice — surfaced in the report.
"""

from __future__ import annotations

import hashlib
import pickle
from datetime import timedelta
from pathlib import Path

import pandas as pd

from backtest.data import CACHE_DIR
from core.logging_setup import get_logger

_log = get_logger("backtest.fundamentals")

REPORTING_LAG_DAYS = 45  # conservative: financials are public ~30-45 days after period end


class FundamentalsStore:
    def __init__(self, revenue: dict[str, pd.Series]) -> None:
        self.revenue = revenue  # symbol -> Series(period_end -> revenue), ascending

    @classmethod
    def fetch(cls, symbols: list[str], force_refresh: bool = False) -> FundamentalsStore:
        key = hashlib.sha1(("fund|" + ",".join(sorted(symbols))).encode()).hexdigest()[:16]
        Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
        cache = CACHE_DIR / f"fund_{key}.pkl"
        if cache.exists() and not force_refresh:
            with cache.open("rb") as fh:
                return cls(pickle.load(fh))  # noqa: S301 - our own cache

        import yfinance as yf

        revenue: dict[str, pd.Series] = {}
        for sym in symbols:
            try:
                q = yf.Ticker(sym).quarterly_income_stmt
                if q is None or "Total Revenue" not in q.index:
                    continue
                ser = q.loc["Total Revenue"].dropna()
                ser.index = pd.DatetimeIndex(ser.index).tz_localize(None).normalize()
                revenue[sym] = ser.sort_index().astype(float)
            except Exception as exc:  # noqa: BLE001
                _log.warning("fundamentals fetch failed for %s: %s", sym, exc)
        with cache.open("wb") as fh:
            pickle.dump(revenue, fh)
        return cls(revenue)

    def growth_asof(self, symbol: str, as_of: str | pd.Timestamp) -> float | None:
        """YoY (preferred) or QoQ revenue growth using only quarters public as-of the date."""
        ser = self.revenue.get(symbol)
        if ser is None or ser.empty:
            return None
        asof_d = pd.Timestamp(as_of).date()
        mask = [(pe.date() + timedelta(days=REPORTING_LAG_DAYS)) <= asof_d for pe in ser.index]
        avail = ser[mask]
        if len(avail) >= 5:
            latest, base = float(avail.iloc[-1]), float(avail.iloc[-5])  # YoY
        elif len(avail) >= 2:
            latest, base = float(avail.iloc[-1]), float(avail.iloc[-2])  # QoQ fallback
        else:
            return None
        return (latest - base) / base if base else None
