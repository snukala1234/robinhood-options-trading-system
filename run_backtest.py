"""Backtest entrypoint -- runs the real pipeline over multi-year real historical bars.

Usage:  python run_backtest.py
Uses the OFFLINE heuristic LLM provider (no API tokens), a simulated broker, PAPER_TRADING=True,
and real daily bars from yfinance (cached under data/backtest_cache/).
"""

from __future__ import annotations

import logging
import os

os.environ.setdefault("TRADING_LLM", "offline")  # never call the real model in a backtest
logging.getLogger().setLevel(logging.WARNING)  # quiet the per-agent INFO logs for a clean report

from backtest.data import HistoricalBarStore  # noqa: E402
from backtest.engine import BacktestConfig, BacktestEngine  # noqa: E402
from backtest.report import compute_report, format_report  # noqa: E402
from config import settings  # noqa: E402

# ~16 large caps across sectors + a few ETFs. SPY is the regime proxy / calendar.
UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD",
    "JPM", "V", "JNJ", "WMT", "XOM", "UNH", "HD", "PG",
    "QQQ", "IWM", "XLK", "XLF",
]
START = "2022-01-01"
END = "2026-07-01"
STARTING_CAPITAL = 2000.0
SLIPPAGE_PER_SIDE = 0.0005  # 5 bps


def _spy_buy_hold(store: HistoricalBarStore, warmup: int) -> float | None:
    dates = store.trading_dates(warmup)
    if not dates:
        return None
    first = store.close_on("SPY", dates[0])
    last = store.close_on("SPY", dates[-1])
    if first and last:
        return (last - first) / first
    return None


def main() -> None:
    cfg = BacktestConfig(
        universe=UNIVERSE, start=START, end=END, starting_capital=STARTING_CAPITAL,
        slippage_per_side=SLIPPAGE_PER_SIDE, db_path=str(settings.REPO_ROOT / "data" / "backtest.db"),
    )
    # Fresh DB each run.
    dbp = settings.REPO_ROOT / "data" / "backtest.db"
    dbp.parent.mkdir(parents=True, exist_ok=True)
    if dbp.exists():
        dbp.unlink()

    print(f"Fetching real daily bars {START}..{END} for {len(UNIVERSE)} symbols + SPY …")
    store = HistoricalBarStore.fetch(UNIVERSE, START, END)
    print(f"Loaded {len(store.bars)} symbols; {len(store.trading_dates(cfg.warmup))} trading days.")

    engine = BacktestEngine(cfg, store=store)
    print("Running walk-forward backtest (offline heuristic signals, PAPER_TRADING=True) …")
    db = engine.run()

    report = compute_report(db, START, END, STARTING_CAPITAL)
    benchmark = _spy_buy_hold(store, cfg.warmup)
    text = format_report(report, benchmark_return=benchmark) + "\n\n" + _caveats()
    out = settings.REPO_ROOT / "data" / "backtest_report.txt"
    out.write_text(text + f"\n\nBacktest DB: {dbp}\n", encoding="utf-8")
    print()
    print(text)
    print(f"\nBacktest DB: {dbp}")
    print(f"Report saved: {out}")
    db.close()


def _caveats() -> str:
    return (
        "TRUST / CAVEATS (read before believing any number above)\n"
        "  1. SIGNALS ARE OFFLINE HEURISTICS, NOT REAL claude-fable-5 REASONING. Technical\n"
        "     (volume/ATR) and macro (regime) react to REAL features; fundamental and\n"
        "     sentiment are per-symbol pseudo-random stand-ins (effectively noise offline).\n"
        "     So ~half the analyst inputs carry no information -- the tested 'edge' is a simple\n"
        "     volume/volatility/regime rule, NOT the production model. Treat returns as a test\n"
        "     of the plumbing, guardrails, sizing, exits and costs -- not as evidence of alpha.\n"
        "  2. SURVIVORSHIP BIAS: the universe is today's large caps; names that dropped out are\n"
        "     excluded, which flatters results.\n"
        "  3. LONG-ONLY, <=3 concurrent positions, settled-cash-only (T+1) -- a deliberately\n"
        "     constrained Phase-1 policy; not a full strategy.\n"
        "  4. COST MODEL: 5 bps slippage per side; commission-free (Robinhood). Stops fill at\n"
        "     the stop (or the open on a gap-down); take-profits fill at the limit (mildly\n"
        "     optimistic). No market-impact/borrow/partial-fill modeling.\n"
        "  5. NOT FITTED, BUT NOT VALIDATED EITHER: heuristic thresholds are arbitrary constants\n"
        "     (no parameter search was run against this data), so there is no classic overfit --\n"
        "     but also no reason to expect them to generalize. Judge by the BY-YEAR/BY-REGIME\n"
        "     split: if it only works in one regime, that's the tell.\n"
        "  6. SAMPLE SIZE: any calibration bucket with n < 30 is not statistically meaningful\n"
        "     (Agent 8 would refuse to act on it); z-scores below that are indicative only."
    )


if __name__ == "__main__":
    main()
