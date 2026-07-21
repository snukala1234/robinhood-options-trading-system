"""Estimate the API cost of running the backtest on a REAL model — WITHOUT calling the API.

Runs the existing harness with a metering client that records call counts and real input-token
sizes, then prices them at Fable 5 / Sonnet 5 / Haiku rates. No network model call is made.
"""

from __future__ import annotations

import logging
import os

os.environ.setdefault("TRADING_LLM", "offline")  # guarantee no real model call
logging.getLogger().setLevel(logging.WARNING)

from backtest.cost import CostMeter, MeteringModelClient, format_estimate  # noqa: E402
from backtest.data import HistoricalBarStore  # noqa: E402
from backtest.engine import BacktestConfig, BacktestEngine  # noqa: E402
from run_backtest import END, START, UNIVERSE  # noqa: E402

SLICE_SYMBOLS = UNIVERSE[:5]
SLICE_START = "2026-01-01"
SLICE_END = "2026-07-01"


def metered_run(universe: list[str], start: str, end: str) -> tuple[CostMeter, int]:
    store = HistoricalBarStore.fetch(universe, start, end)
    cfg = BacktestConfig(universe=universe, start=start, end=end, db_path=":memory:")
    meter = CostMeter()
    engine = BacktestEngine(cfg, store=store, client=MeteringModelClient(meter))
    engine.run()
    return meter, len(store.trading_dates(cfg.warmup))


def main() -> None:
    print("Metering FULL run (no API calls) …")
    full, full_days = metered_run(UNIVERSE, START, END)
    print("Metering SLICE run (no API calls) …")
    sl, slice_days = metered_run(SLICE_SYMBOLS, SLICE_START, SLICE_END)

    print()
    print(format_estimate(f"FULL: {len(UNIVERSE)} symbols, {START}..{END} ({full_days} days)", full))
    print()
    print(format_estimate(
        f"SLICE: {len(SLICE_SYMBOLS)} symbols, {SLICE_START}..{SLICE_END} ({slice_days} days)", sl))
    print()
    print(_notes(full))


def _notes(full: CostMeter) -> str:
    exit_calls = full.per_agent_calls.get("exit_monitor", 0)
    exit_share = 100 * exit_calls / full.calls if full.calls else 0
    return (
        "NOTES / how to read this\n"
        "  * Input tokens are MEASURED from the real prompts the agents build; output tokens\n"
        "    are ESTIMATED (offline can't generate), hence the low/high band. Real structured\n"
        "    JSON + a one-sentence reasoning field is typically ~75-350 output tokens/call.\n"
        f"  * The exit monitor is {exit_share:.0f}% of all calls: the harness probes each open\n"
        "    position 3x/day (low, high, close) and each probe runs the thesis LLM call. In a\n"
        "    real-model backtest you'd set ExitMonitor(thesis_enabled=False) (offline thesis is\n"
        "    inert anyway) or call it once/day — that alone removes most of the cost.\n"
        "  * The scanner runs full research (4 sub-agents + aggregator) on up to 8 candidates\n"
        "    EVERY day regardless of whether a trade results — the other big driver. Caching\n"
        "    research by (symbol, day) or narrowing the candidate set would cut this sharply.\n"
        "  * Prompt caching is NOT modeled: system prompts repeat every call, so Anthropic\n"
        "    prompt caching could cut input cost materially (cached reads bill ~10%). These\n"
        "    figures are therefore an UPPER bound on input cost.\n"
        "  * Narrative-only calls (portfolio construction, risk manager) and the Agent 8\n"
        "    auditor summary are non-authoritative; they can run on a cheaper model or be\n"
        "    disabled in a backtest with no effect on the trade decisions."
    )


if __name__ == "__main__":
    main()
