"""Real-vs-offline slice comparison (6 months, 5 symbols) with the two efficiency fixes on.

Because no ANTHROPIC_API_KEY is available, the LIVE claude-sonnet-5 run cannot execute here.
Instead this runs the pipeline with REAL point-in-time fundamentals (yfinance, lagged) vs the
offline-noise baseline on the SAME data, and diffs decisions — a runnable, look-ahead-safe
answer to "did real fundamental data change entries/exits?" Sentiment is neutralized (no real
historical news data is obtainable here). Broker simulated, PAPER_TRADING=True, guardrails intact.
"""

from __future__ import annotations

import logging
import os

os.environ.setdefault("TRADING_LLM", "offline")
os.environ.setdefault("TRADING_MARKET_DATA", "offline")
logging.getLogger().setLevel(logging.WARNING)

from backtest.compare import compare_probes, format_comparison, probe_signals  # noqa: E402
from backtest.cost import CostMeter, MeteringModelClient  # noqa: E402
from backtest.data import HistoricalBarStore  # noqa: E402
from backtest.engine import BacktestConfig, BacktestEngine  # noqa: E402
from backtest.report import compute_report, format_report  # noqa: E402
from core.db import Database  # noqa: E402

SYMBOLS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL"]
START = "2026-01-01"
END = "2026-07-01"
CAPITAL = 2000.0


def _run(store: HistoricalBarStore, fundamental_mode: str, sentiment_mode: str) -> Database:
    cfg = BacktestConfig(universe=SYMBOLS, start=START, end=END, starting_capital=CAPITAL,
                         db_path=":memory:", fundamental_mode=fundamental_mode,
                         sentiment_mode=sentiment_mode)
    return BacktestEngine(cfg, store=store).run()


def _spy_bh(store: HistoricalBarStore, warmup: int = 25) -> float | None:
    dates = store.trading_dates(warmup)
    if not dates:
        return None
    a, b = store.close_on("SPY", dates[0]), store.close_on("SPY", dates[-1])
    return (b - a) / a if a and b else None


def main() -> None:
    store = HistoricalBarStore.fetch(SYMBOLS, START, END)
    bench = _spy_bh(store)

    # Show the effect of the two efficiency fixes on call volume.
    meter = CostMeter()
    BacktestEngine(BacktestConfig(universe=SYMBOLS, start=START, end=END, db_path=":memory:"),
                   store=store, client=MeteringModelClient(meter)).run()
    print(f"Model calls WITH both fixes: {meter.calls:,}  "
          f"(was 1,837 before fixes — thesis 3x/day probing + daily scan-when-full removed)")
    print(f"exit_monitor calls now: {meter.per_agent_calls.get('exit_monitor', 0)} "
          f"(was 703);  scanner calls now: {meter.per_agent_calls.get('scanner', 0)} (was 98)\n")

    base_db = _run(store, "offline", "offline")
    real_db = _run(store, "real", "neutral")

    print(format_report(compute_report(base_db, START, END, CAPITAL), bench)
          .replace("BACKTEST REPORT", "BASELINE (offline-noise fundamental+sentiment)"))
    print()
    print(format_report(compute_report(real_db, START, END, CAPITAL), bench)
          .replace("BACKTEST REPORT", "VARIANT (REAL fundamentals + neutral sentiment)"))
    print()

    base_probe = probe_signals(store, SYMBOLS, "offline", "offline")
    real_probe = probe_signals(store, SYMBOLS, "real", "neutral")
    print(format_comparison(compare_probes(base_probe, real_probe), base_db, real_db))
    print()
    print(_blockers())


def _blockers() -> str:
    return (
        "WHAT COULD NOT BE DONE HERE (honest blockers)\n"
        "  1. LIVE claude-sonnet-5 was NOT run: no ANTHROPIC_API_KEY is set in this environment,\n"
        "     so no real model call was made. The plumbing IS ready: set the key and run with\n"
        "     TRADING_LLM=live (config/models.py already routes; per-agent model is swappable).\n"
        "  2. REAL sentiment could NOT be made point-in-time: yfinance .news returns only ~10\n"
        "     current headlines with no usable timestamps, and no free look-ahead-safe historical\n"
        "     news feed exists here. Sentiment is therefore NEUTRALIZED in the variant (not real).\n"
        "     A real run needs a timestamped news+sentiment vendor.\n"
        "  3. REAL fundamentals are genuine but SPARSE: yfinance exposes only ~5 recent quarters,\n"
        "     so with the 45-day filing lag the fundamental signal is mostly available only in the\n"
        "     later part of this 6-month window (YoY needs 5 quarters; QoQ is used as a fallback).\n"
        "  4. Because a real model was not used, the offline heuristics still stand in for the\n"
        "     model's reasoning on the technical/macro agents — see the earlier caveats. This\n"
        "     comparison isolates the *fundamental-data* channel, not full model reasoning."
    )


if __name__ == "__main__":
    main()
