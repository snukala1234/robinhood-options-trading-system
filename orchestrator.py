"""Orchestrator (Portfolio Manager) — wires all eight agents end-to-end in paper mode.

Pipeline per Section 2: Scanner -> Research(4) -> Edge Aggregator -> Portfolio Construction
-> Risk Manager -> Execution, with the Exit Monitor managing open positions and Agent 8
auditing. Runs strictly in paper mode (``PAPER_TRADING = True``); every guardrail decision is
pure code. Section 3.8 policy is enforced: no new capital is committed on a decision made
under a failover model.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from agents.auditor import CalibrationAuditor, PromotionOutcome, auto_hold_confirmer
from agents.edge_aggregator import EdgeAggregator
from agents.execution import STATUS_FILLED, ExecutionAgent
from agents.exit_monitor import ACTION_EXIT, ExitMonitor
from agents.portfolio_construction import PortfolioConstruction
from agents.research.base import ResearchAgent, ResearchContext
from agents.research.fundamental import FundamentalResearchAgent
from agents.research.macro import MacroResearchAgent
from agents.research.sentiment import SentimentResearchAgent
from agents.research.technical import TechnicalResearchAgent
from agents.risk_manager import RiskManager
from agents.scanner import Scanner
from config.guardrails import MAX_CONCURRENT_POSITIONS, PAPER_TRADING
from config.settings import DEFAULT_UNIVERSE
from config.strategy import DEFAULT_STRATEGY
from core.db import Database
from core.event_bus import bus, publish_agent_status, publish_equity_tick
from core.logging_setup import get_logger, log_decision
from core.market_data import get_market_regime, get_snapshot
from core.records import Position
from core.util import parse_iso
from risk.sizing import Account, Sale, settled_cash

_log = get_logger("orchestrator")

ORCH_KEY = "orchestrator"

PriceFn = Callable[[str], float]


@dataclass
class PaperPortfolio:
    """Cash-account paper portfolio: settled cash, unsettled (T+1) sale proceeds, HWM."""

    starting_capital: float
    settled_cash: float = 0.0
    pending_sales: list[Sale] = field(default_factory=list)
    high_water_mark: float = 0.0
    daily_start_equity: float = 0.0

    def __post_init__(self) -> None:
        self.settled_cash = self.settled_cash or self.starting_capital
        self.high_water_mark = self.high_water_mark or self.starting_capital
        self.daily_start_equity = self.daily_start_equity or self.starting_capital

    def account(self) -> Account:
        return Account(cleared_deposits=self.settled_cash, recent_sales=list(self.pending_sales))

    def settled_available(self, now: datetime) -> float:
        return settled_cash(self.account(), now)

    def unsettled_total(self) -> float:
        return round(sum(s.proceeds for s in self.pending_sales), 2)

    def on_buy(self, notional: float) -> None:
        self.settled_cash = round(self.settled_cash - notional, 2)

    def on_sell(self, proceeds: float, settlement_date: datetime) -> None:
        self.pending_sales.append(Sale(proceeds=proceeds, settlement_date=settlement_date.date()))

    def settle(self, now: datetime) -> None:
        matured = [s for s in self.pending_sales if s.settlement_date <= now.date()]
        self.settled_cash = round(self.settled_cash + sum(s.proceeds for s in matured), 2)
        self.pending_sales = [s for s in self.pending_sales if s.settlement_date > now.date()]

    def equity(self, open_positions: list[Position], price_of: PriceFn) -> float:
        positions_value = sum(p.shares * price_of(p.symbol) for p in open_positions)
        return round(self.settled_cash + self.unsettled_total() + positions_value, 2)

    def open_positions_value(self, open_positions: list[Position], price_of: PriceFn) -> float:
        return round(sum(p.shares * price_of(p.symbol) for p in open_positions), 2)

    def update_hwm(self, equity: float) -> None:
        self.high_water_mark = max(self.high_water_mark, equity)


class Orchestrator:
    def __init__(
        self,
        db: Database,
        client: object,
        portfolio: PaperPortfolio,
        universe: list[str] | None = None,
        paper: bool | None = None,
    ) -> None:
        self.db = db
        self.client = client
        self.portfolio = portfolio
        self.paper = PAPER_TRADING if paper is None else paper
        self.universe = list(universe) if universe else list(DEFAULT_UNIVERSE)

        self.scanner = Scanner(client, db, self.universe)
        self.research: list[ResearchAgent] = [
            TechnicalResearchAgent(client, db),
            FundamentalResearchAgent(client, db),
            SentimentResearchAgent(client, db),
            MacroResearchAgent(client, db),
        ]
        self.aggregator = EdgeAggregator(client, db)
        self.construction = PortfolioConstruction(client, db)
        self.risk = RiskManager(client, db)
        self.execution = ExecutionAgent(broker=None, db=db, paper=self.paper)
        self.exit_monitor = ExitMonitor(client, db)
        self.auditor = CalibrationAuditor(db, client)

        self._ensure_active_config()
        bus.subscribe(self._persist_status)

    # -- setup -------------------------------------------------------------

    def _ensure_active_config(self) -> str:
        active = self.db.active_config()
        if active is not None:
            return str(active["id"])
        cid = self.db.insert_config_version(
            parameters={"strategy": DEFAULT_STRATEGY.to_dict(), "ensemble_weights": {},
                        "recalibration": {}},
            promoted_by="human_confirmed", proposed_by_agent="bootstrap", is_active=True,
        )
        self.db.set_active_config(cid)
        return cid

    def _persist_status(self, event: dict[str, object]) -> None:
        if event.get("type") == "agent_status":
            self.db.upsert_agent_status(
                agent_name=str(event.get("agent_name")), state=str(event.get("state")),
                summary=_opt_str(event.get("summary")), active_model=_opt_str(event.get("active_model")),
                ts=_opt_str(event.get("ts")),
            )

    # -- entry cycle -------------------------------------------------------

    def run_entry_cycle(
        self, now: datetime, regime: str | None = None, price_of: PriceFn | None = None,
    ) -> list[dict[str, object]]:
        publish_agent_status(ORCH_KEY, "running", "entry cycle", active_model=None)
        regime = regime or get_market_regime()
        price = price_of or _snapshot_price
        candidates = self.scanner.scan()
        outcomes: list[dict[str, object]] = []

        for cand in candidates:
            open_positions = self.db.open_positions()
            if len(open_positions) >= MAX_CONCURRENT_POSITIONS:
                break  # concurrency cap; wait for an exit
            if any(p.symbol == cand.symbol for p in open_positions):
                continue  # already hold this name; one position per symbol

            ctx = ResearchContext(cand.symbol, cand.snapshot, regime)
            signals = [agent.analyze(ctx) for agent in self.research]
            agg = self.aggregator.aggregate(cand.symbol, signals, cand.snapshot, regime)
            # Enter at the current (live/simulated) price, not the static snapshot price.
            agg.current_price = price(cand.symbol)

            # Section 3.8: do NOT commit new capital on a failover-model decision.
            if agg.decided_under_failover:
                outcomes.append({"symbol": cand.symbol, "result": "skipped_failover"})
                log_decision(_log, "entry_skipped_failover", symbol=cand.symbol)
                continue

            equity = self.portfolio.equity(open_positions, price)
            settled = self.portfolio.settled_available(now)
            proposal = self.construction.build(agg, equity, settled, open_positions)
            decision = self.risk.evaluate(
                proposal, self.portfolio.account(), now, equity,
                self.portfolio.high_water_mark, self.portfolio.daily_start_equity, open_positions,
            )
            if not decision.approved:
                outcomes.append({"symbol": cand.symbol, "result": f"blocked:{decision.reason}"})
                continue

            result = self.execution.place_entry(
                proposal.symbol, proposal.shares, proposal.size_usd, proposal.entry_price,
                self.portfolio.account(), now,
            )
            if result.status == STATUS_FILLED and result.fill is not None:
                trade_id = self._open_trade(proposal, agg, now)
                self.portfolio.on_buy(proposal.size_usd)
                self.db.mark_signal_traded(cand.symbol, trade_id)
                outcomes.append({"symbol": cand.symbol, "result": "opened", "trade_id": trade_id,
                                 "size_usd": proposal.size_usd})
                self.snapshot_equity(price, "post_trade", now)
            else:
                outcomes.append({"symbol": cand.symbol, "result": f"order_{result.status}"})

        publish_agent_status(ORCH_KEY, "idle", f"entry cycle done ({len(outcomes)})",
                             active_model=None)
        return outcomes

    def _open_trade(self, proposal: object, agg: object, now: datetime) -> str:
        p = proposal  # PortfolioConstruction.TradeProposal
        a = agg  # AggregatedSignal
        active = self.db.active_config()
        return self.db.insert_trade_entry(
            symbol=p.symbol, entry_price=p.entry_price, position_size_usd=p.size_usd,  # type: ignore[attr-defined]
            shares=p.shares, contributing_agents=a.contributing,  # type: ignore[attr-defined]
            aggregated_confidence=p.aggregated_confidence,  # type: ignore[attr-defined]
            account_equity_at_entry=self.portfolio.daily_start_equity,
            atr_pct_at_entry=p.atr_pct, market_regime_at_entry=p.market_regime,  # type: ignore[attr-defined]
            stop_loss_pct=p.stop_loss_pct, take_profit_pct=p.take_profit_pct,  # type: ignore[attr-defined]
            config_version_id=str(active["id"]) if active else None,
            active_model=p.active_model, decided_under_failover=p.decided_under_failover,  # type: ignore[attr-defined]
            is_paper=self.paper, entry_ts=now.isoformat(),
        )

    # -- monitor / exit cycle ---------------------------------------------

    def run_monitor_cycle(
        self, price_of: PriceFn, now: datetime, scheduled_review: bool = False,
        regime: str | None = None,
    ) -> list[dict[str, object]]:
        regime = regime or get_market_regime()
        self.portfolio.settle(now)  # mature any T+1 proceeds first
        open_positions = self.db.open_positions()
        decisions = self.exit_monitor.evaluate_all(open_positions, price_of, regime, scheduled_review)
        outcomes: list[dict[str, object]] = []

        for d in decisions:
            if d.action != ACTION_EXIT:
                continue
            result = self.execution.place_exit(d.position, d.current_price, now)
            if result.status == STATUS_FILLED and result.fill is not None:
                self._close_trade(d.position, d.current_price, d.reason, now)
                proceeds = round(d.position.shares * d.current_price, 2)
                self.portfolio.on_sell(proceeds, settlement_date=now + timedelta(days=1))
                outcomes.append({"symbol": d.position.symbol, "reason": d.reason,
                                 "exit_price": d.current_price})

        self.snapshot_equity(price_of, "monitor_tick", now)
        return outcomes

    def _close_trade(self, position: Position, exit_price: float, reason: str, now: datetime) -> None:
        pnl = round((exit_price - position.entry_price) * position.shares, 2)
        try:
            entry_dt = parse_iso(position.entry_ts)
            holding_hours = round((now - entry_dt).total_seconds() / 3600.0, 4)
        except ValueError:
            holding_hours = 0.0
        self.db.close_trade(position.trade_id, exit_price=exit_price, exit_reason=reason,
                            realized_pnl=pnl, holding_period_hours=holding_hours,
                            exit_ts=now.isoformat())

    # -- equity + daily pnl -----------------------------------------------

    def snapshot_equity(self, price_of: PriceFn, source: str, now: datetime) -> float:
        open_positions = self.db.open_positions()
        equity = self.portfolio.equity(open_positions, price_of)
        self.portfolio.update_hwm(equity)
        snap_id = self.db.insert_equity_snapshot(
            total_equity=equity, settled_cash=self.portfolio.settled_available(now),
            open_positions_value=self.portfolio.open_positions_value(open_positions, price_of),
            high_water_mark=self.portfolio.high_water_mark,
            open_position_count=len(open_positions), source=source, is_paper=self.paper,
            ts=now.isoformat(),
        )
        publish_equity_tick({
            "snapshot_id": snap_id, "ts": now.isoformat(), "total_equity": equity,
            "settled_cash": self.portfolio.settled_available(now),
            "high_water_mark": self.portfolio.high_water_mark, "is_paper": self.paper,
        })
        return equity

    def rollup_daily_pnl(self, trading_date: str, price_of: PriceFn, now: datetime) -> None:
        trades = self.db.trades_on_date(trading_date)
        closed_today = [t for t in trades if str(t.get("exit_ts") or "")[:10] == trading_date]
        opened_today = [t for t in trades if str(t.get("entry_ts") or "")[:10] == trading_date]
        realized = round(sum(float(t.get("realized_pnl") or 0.0) for t in closed_today), 2)
        wins = sum(1 for t in closed_today if float(t.get("realized_pnl") or 0.0) > 0)
        losses = sum(1 for t in closed_today if float(t.get("realized_pnl") or 0.0) < 0)

        ending = self.snapshot_equity(price_of, "scheduled_close", now)
        starting = self.portfolio.daily_start_equity
        total = round(ending - starting, 2)
        self.db.upsert_daily_pnl(
            trading_date=trading_date, starting_equity=starting, ending_equity=ending,
            realized_pnl=realized, unrealized_pnl_change=round(total - realized, 2),
            total_pnl=total, total_pnl_pct=round(total / starting, 6) if starting else 0.0,
            trades_opened=len(opened_today), trades_closed=len(closed_today),
            wins=wins, losses=losses, is_paper=self.paper,
        )
        # Next session starts from today's close.
        self.portfolio.daily_start_equity = ending

    # -- learning loop -----------------------------------------------------

    def run_audit(self, confirmer: Callable[[dict[str, object]], bool] = auto_hold_confirmer) -> PromotionOutcome:
        return self.auditor.run_audit(confirmer)


def _snapshot_price(symbol: str) -> float:
    """Default price source: the current market-data snapshot price (static offline)."""
    return get_snapshot(symbol).current_price


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)
