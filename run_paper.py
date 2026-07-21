"""Paper-mode entrypoint — runs the full 8-agent pipeline end-to-end.

Hermetic by default: offline deterministic market data + offline model provider, so it runs
with no network and no credentials. ``PAPER_TRADING = True`` and ``ORDER_APPROVAL_MODE =
"manual"`` throughout — no code path places, funds, or confirms a live order.

Usage:
    python run_paper.py            # fresh paper run, prints a summary
    TRADING_LLM=live python run_paper.py   # (only if you have ANTHROPIC_API_KEY) real Fable 5
"""

from __future__ import annotations

import logging
import math
import os
from datetime import UTC, datetime, timedelta

# Default to a hermetic run unless the operator opted into live data/models.
os.environ.setdefault("TRADING_MARKET_DATA", "offline")
os.environ.setdefault("TRADING_LLM", "offline")

from config import settings  # noqa: E402
from config.guardrails import ORDER_APPROVAL_MODE, PAPER_TRADING  # noqa: E402
from core.db import Database  # noqa: E402
from core.llm import get_client  # noqa: E402
from core.logging_setup import configure_logging  # noqa: E402
from core.market_data import get_market_regime, get_snapshot  # noqa: E402
from orchestrator import Orchestrator, PaperPortfolio  # noqa: E402

STARTING_CAPITAL = 300.0  # within Phase 1 ($100–$500)
SESSIONS = 14


def _set_session_prices(
    prices: dict[str, float], base: dict[str, float], session: int
) -> None:
    """Deterministic oscillating price path (±33%) around each symbol's base price.

    Swinging past both the +25% take-profit and -18% stop guarantees the full
    open -> monitor -> close lifecycle (both wins and losses) is exercised across sessions,
    reproducibly and without randomness. This is a paper price simulator, not strategy logic.
    """
    for sym in base:
        phase = sum(ord(c) for c in sym) % 7
        prices[sym] = round(base[sym] * (1.0 + 0.33 * math.sin(0.9 * session + phase)), 4)


def main() -> None:
    configure_logging(level=logging.WARNING)  # keep the summary readable

    # Fresh paper database for a reproducible run.
    db_path = settings.DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    db = Database.connect(db_path)

    client = get_client(db)
    portfolio = PaperPortfolio(STARTING_CAPITAL)
    orch = Orchestrator(db, client, portfolio)

    universe = orch.universe
    base_prices = {sym: get_snapshot(sym).current_price for sym in universe}
    prices = dict(base_prices)
    price_of = lambda s: prices[s]  # noqa: E731
    regime = get_market_regime()

    start = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
    print(f"=== PAPER RUN ===  capital=${STARTING_CAPITAL:.0f}  regime={regime}  "
          f"PAPER_TRADING={PAPER_TRADING}  approval={ORDER_APPROVAL_MODE}")

    total_opened = 0
    total_closed = 0
    for i in range(SESSIONS):
        now = start + timedelta(days=i)
        _set_session_prices(prices, base_prices, i)  # this session's marks
        # Morning: settle T+1 proceeds and manage exits at current prices.
        exits = orch.run_monitor_cycle(price_of, now, regime=regime)
        total_closed += len(exits)
        # Then look for new entries with settled cash.
        entries = orch.run_entry_cycle(now, regime=regime, price_of=price_of)
        opened = [o for o in entries if o.get("result") == "opened"]
        total_opened += len(opened)
        # End-of-day rollup.
        orch.rollup_daily_pnl(now.date().isoformat(), price_of, now)
        if opened or exits:
            print(f"  {now.date()}: opened={[o['symbol'] for o in opened]} "
                  f"exits={[(e['symbol'], e['reason']) for e in exits]}")

    # Final state.
    open_positions = db.open_positions()
    final_equity = portfolio.equity(open_positions, price_of)
    closed = db.closed_trades()
    wins = sum(1 for t in closed if float(t.get("realized_pnl") or 0) > 0)

    print("\n=== SUMMARY ===")
    print(f"trades opened={total_opened} closed={len(closed)} wins={wins} "
          f"losses={len(closed) - wins}")
    print(f"open positions now={len(open_positions)} "
          f"({[p.symbol for p in open_positions]})")
    print(f"settled=${portfolio.settled_available(start + timedelta(days=SESSIONS)):.2f} "
          f"unsettled=${portfolio.unsettled_total():.2f} "
          f"equity=${final_equity:.2f} (start ${STARTING_CAPITAL:.0f}) "
          f"HWM=${portfolio.high_water_mark:.2f}")
    print(f"equity_snapshots={len(db.equity_series())} daily_pnl_rows="
          f"{len(db.daily_pnl_range('2026-01-01', '2026-12-31'))}")

    # Agent 8 audit (Phase 1: auto-hold, human confirmation required to promote).
    outcome = orch.run_audit()
    buckets = db.latest_calibration_buckets()
    print("\n=== AGENT 8 AUDIT ===")
    print(f"decision={outcome.decision}  calibration_buckets={len(buckets)}")
    for b in buckets[:6]:
        print(f"  {b['source_agent']:22s} {b['confidence_band']:10s} "
              f"n={b['sample_size']} obs={b['observed_hit_rate']:.2f} "
              f"exp={b['expected_hit_rate']:.2f} z={b['z_score']:.2f}")
    print("\nMIN_SAMPLE_SIZE_FOR_ADAPTATION not yet cleared -> no live parameter change "
          "(correct Phase-1 behavior).")
    print(f"\nDB written to: {db_path}")
    db.close()


if __name__ == "__main__":
    main()
