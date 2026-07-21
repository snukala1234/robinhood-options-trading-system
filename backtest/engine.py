"""Walk-forward backtest engine.

Drives the UNCHANGED pipeline day by day over historical bars:
- Entries go through the real ``Orchestrator.run_entry_cycle`` (Scanner -> Research -> Edge ->
  Construction -> Risk -> Execution), seeing only as-of-date snapshots.
- Exits are detected by the real ``ExitMonitor`` / Section-1 stop logic, but evaluated against
  each day's ACTUAL OHLC (low for stops, high for take-profit) — the harness only chooses which
  of the day's prices to check and applies slippage/gap fills; it never changes exit logic.
- T+1 settlement and sizing come from the unchanged ``PaperPortfolio`` / ``risk.sizing``.

No look-ahead: every read on day D uses bars with date <= D only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pandas as pd

from agents.execution import STATUS_FILLED
from agents.exit_monitor import ACTION_EXIT, REASON_STOP_LOSS, REASON_TAKE_PROFIT, ExitMonitor
from agents.research.base import ResearchAgent
from agents.research.fundamental import FundamentalResearchAgent
from agents.research.macro import MacroResearchAgent
from agents.research.sentiment import SentimentResearchAgent
from agents.research.technical import TechnicalResearchAgent
from backtest.data import HistoricalBarStore
from backtest.variants import NeutralResearchAgent, RealFundamentalAgent
from config.guardrails import MAX_CONCURRENT_POSITIONS
from core import market_data
from core.db import Database
from core.llm import ModelClient, OfflineProvider
from core.logging_setup import configure_logging, get_logger
from core.market_data import get_market_regime
from orchestrator import Orchestrator, PaperPortfolio

_log = get_logger("backtest.engine")


@dataclass
class BacktestConfig:
    universe: list[str]
    start: str
    end: str
    starting_capital: float = 3000.0
    slippage_per_side: float = 0.0005  # 5 bps each way (~10 bps round trip)
    warmup: int = 25
    db_path: str = ":memory:"
    calendar_symbol: str = "SPY"
    # --- backtest-only efficiency fixes (behaviour-preserving) ---
    thesis_enabled: bool = False        # fix 1: inert offline; disabling removes the LLM call
    skip_scan_when_full: bool = True    # fix 2: don't scan when already at the position cap
    # --- data variants for the fundamental / sentiment sub-agents ---
    fundamental_mode: str = "offline"   # "offline" (RNG) | "real" (yfinance) | "neutral"
    sentiment_mode: str = "offline"     # "offline" (RNG) | "neutral"


class BacktestEngine:
    def __init__(
        self, config: BacktestConfig, store: HistoricalBarStore | None = None,
        client: object | None = None,
    ) -> None:
        configure_logging(level=logging.WARNING)
        self.cfg = config
        self.store = store or HistoricalBarStore.fetch(
            config.universe, config.start, config.end, config.calendar_symbol
        )
        # Trading universe excludes the regime proxy.
        universe = [s for s in config.universe if s in self.store.bars
                    and s != self.store.calendar_symbol]
        self.db = Database.connect(config.db_path)
        self.portfolio = PaperPortfolio(config.starting_capital)
        # Offline client by default (no API tokens); a metering client may be injected.
        self.client = client if client is not None else ModelClient(
            provider=OfflineProvider(), db=self.db
        )
        self.orch = Orchestrator(self.db, self.client, self.portfolio, universe=universe, paper=True)
        self.slip = config.slippage_per_side
        self._days = 0

        # Fix 1: disable the (offline-inert) thesis LLM call in the backtest.
        if not config.thesis_enabled:
            self.orch.exit_monitor = ExitMonitor(self.client, self.db, thesis_enabled=False)

        # Swap in the fundamental / sentiment sub-agent variants requested by config.
        self.fund_store = None
        if config.fundamental_mode == "real":
            from backtest.fundamentals import FundamentalsStore
            self.fund_store = FundamentalsStore.fetch(universe)
        self.orch.research = self._build_research(universe)

    def _build_research(self, universe: list[str]) -> list[ResearchAgent]:
        tech = TechnicalResearchAgent(self.client, self.db)
        macro = MacroResearchAgent(self.client, self.db)

        fundamental: ResearchAgent
        if self.cfg.fundamental_mode == "real" and self.fund_store is not None:
            fundamental = RealFundamentalAgent(self.client, self.db, self.fund_store)
        elif self.cfg.fundamental_mode == "neutral":
            fundamental = NeutralResearchAgent(self.client, self.db, "research_fundamental")
        else:
            fundamental = FundamentalResearchAgent(self.client, self.db)

        sentiment: ResearchAgent
        if self.cfg.sentiment_mode == "neutral":
            sentiment = NeutralResearchAgent(self.client, self.db, "research_sentiment")
        else:
            sentiment = SentimentResearchAgent(self.client, self.db)

        return [tech, fundamental, sentiment, macro]

    # -- pricing helpers ---------------------------------------------------

    def _mark(self, date: pd.Timestamp, symbol: str) -> float:
        c = self.store.close_on(symbol, date)
        if c is not None:
            return c
        snap = self.store.snapshot_asof(symbol, date)
        return snap.current_price if snap else 0.0

    def _entry_price(self, date: pd.Timestamp, symbol: str) -> float:
        # Buy fill: pay up by one slippage step.
        return round(self._mark(date, symbol) * (1.0 + self.slip), 4)

    # -- main loop ---------------------------------------------------------

    def run(self) -> Database:
        market_data.set_snapshot_provider(self.store.provider)
        try:
            for date in self.store.trading_dates(self.cfg.warmup):
                self._run_day(date)
        finally:
            market_data.clear_snapshot_provider()
        return self.db

    def _run_day(self, date: pd.Timestamp) -> None:
        self.store.set_current_date(date)
        now = datetime(date.year, date.month, date.day, 20, 0, tzinfo=UTC)
        regime = get_market_regime(self.cfg.calendar_symbol)

        # 1. Settle matured T+1 proceeds (unchanged portfolio logic).
        self.portfolio.settle(now)
        # 2. Manage exits against this day's ACTUAL OHLC.
        self._process_exits(date, now, regime)
        # 3. New entries through the real pipeline (as-of snapshots only).
        #    Fix 2: skip the whole entry cycle (incl. the scanner LLM call) when already at the
        #    position cap — no entry is possible, so it is pure wasted compute/cost.
        at_cap = self.db.open_position_count() >= MAX_CONCURRENT_POSITIONS
        if not (self.cfg.skip_scan_when_full and at_cap):
            self.orch.run_entry_cycle(
                now, regime=regime, price_of=lambda s: self._entry_price(date, s)
            )
        # 4. Mark-to-market close + daily rollup (raw closes, no slippage on marks).
        mark = lambda s: self._mark(date, s)  # noqa: E731
        self.orch.snapshot_equity(mark, "scheduled_close", now)
        self.orch.rollup_daily_pnl(date.date().isoformat(), mark, now)
        self._days += 1

    def _process_exits(self, date: pd.Timestamp, now: datetime, regime: str) -> None:
        monitor = self.orch.exit_monitor
        for pos in self.db.open_positions():
            bar = self.store.bar_on(pos.symbol, date)
            if bar is None:
                continue  # no bar this day -> cannot act (holiday for this name)
            stop_price = pos.entry_price * (1 - pos.stop_loss_pct)
            tp_price = pos.entry_price * (1 + pos.take_profit_pct)

            reason: str | None = None
            fill: float | None = None

            # Detect the STOP with the real monitor, using the day's LOW.
            d_low = monitor.evaluate_position(pos, bar.low, regime)
            if d_low.action == ACTION_EXIT and d_low.reason == REASON_STOP_LOSS:
                reason = REASON_STOP_LOSS
                # Gap risk: if it opened below the stop, you fill at the (worse) open.
                base = min(stop_price, bar.open) if bar.open < stop_price else stop_price
                fill = base * (1 - self.slip)
            else:
                # Otherwise detect TAKE-PROFIT using the day's HIGH.
                d_high = monitor.evaluate_position(pos, bar.high, regime)
                if d_high.action == ACTION_EXIT and d_high.reason == REASON_TAKE_PROFIT:
                    reason = REASON_TAKE_PROFIT
                    base = max(tp_price, bar.open) if bar.open > tp_price else tp_price
                    fill = base * (1 - self.slip)
                else:
                    # Finally, a thesis/scheduled exit decided at the close (offline: rare).
                    d_close = monitor.evaluate_position(pos, bar.close, regime)
                    if d_close.action == ACTION_EXIT:
                        reason = d_close.reason
                        fill = bar.close * (1 - self.slip)

            if reason is None or fill is None:
                continue
            fill = round(fill, 4)
            result = self.orch.execution.place_exit(pos, fill, now)
            if result.status == STATUS_FILLED:
                self.orch._close_trade(pos, fill, reason, now)
                self.portfolio.on_sell(round(pos.shares * fill, 2), settlement_date=now + timedelta(days=1))

    @property
    def days_simulated(self) -> int:
        return self._days
