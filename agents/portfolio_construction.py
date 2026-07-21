"""Agent 4 — Portfolio Construction.

Sizing is PURE CODE (Section 1 via :mod:`risk.sizing`) — no LLM touches the dollar amount.
Fable 5 is used only for a non-authoritative construction *rationale* narrative (routed
through ``config.models``), never to compute or approve size. Cash-account rule: long-only
(no shorting), and only calibrated confidence >= the Section 0 gate may propose a trade.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from config.guardrails import HARD_STOP_LOSS_PCT
from config.strategy import DEFAULT_STRATEGY, StrategyParams
from core.event_bus import publish_agent_status, publish_signal_flow
from core.logging_setup import get_logger, log_decision
from core.records import LONG, SHORT, AggregatedSignal, Position
from risk.sizing import calculate_position_size, passes_confidence_gate

_log = get_logger("portfolio_construction")

AGENT_KEY = "portfolio_construct"


@dataclass
class TradeProposal:
    symbol: str
    direction: str
    entry_price: float
    size_usd: float
    shares: float
    stop_loss_pct: float
    take_profit_pct: float
    aggregated_confidence: float
    atr_pct: float
    market_regime: str
    active_model: str
    decided_under_failover: bool
    viable: bool
    reason: str
    contributing: dict[str, dict[str, object]] = field(default_factory=dict)
    rationale: str = ""


class PortfolioConstruction:
    def __init__(
        self,
        client: object,
        db: object | None = None,
        params: StrategyParams = DEFAULT_STRATEGY,
    ) -> None:
        self.client = client
        self.db = db
        self.params = params

    def build(
        self,
        signal: AggregatedSignal,
        account_equity: float,
        settled_cash_amount: float,
        open_positions: list[Position],
    ) -> TradeProposal:
        publish_agent_status(AGENT_KEY, "running", f"sizing {signal.symbol}", active_model=None)
        atr_pct = signal.atr_14 / signal.current_price if signal.current_price else 0.0

        def reject(reason: str) -> TradeProposal:
            log_decision(_log, "proposal_rejected", symbol=signal.symbol, reason=reason)
            publish_agent_status(AGENT_KEY, "idle", f"{signal.symbol} rejected: {reason}",
                                 active_model=None)
            return TradeProposal(
                symbol=signal.symbol, direction=signal.direction, entry_price=signal.current_price,
                size_usd=0.0, shares=0.0, stop_loss_pct=HARD_STOP_LOSS_PCT,
                take_profit_pct=self.params.take_profit_pct,
                aggregated_confidence=signal.calibrated_confidence, atr_pct=atr_pct,
                market_regime=signal.market_regime, active_model=signal.active_model,
                decided_under_failover=signal.decided_under_failover, viable=False, reason=reason,
                contributing=signal.contributing,
            )

        # Cash account: long-only. No shorting; a short view is simply not actionable here.
        if signal.direction == SHORT:
            return reject("short_not_allowed_cash_account")
        if signal.direction != LONG:
            return reject("no_directional_edge")

        # Section 0 confidence gate (pure code, not LLM).
        if not passes_confidence_gate(signal.calibrated_confidence):
            return reject("below_min_confidence")

        # Pure-code Section 1 sizing (caps at settled cash AND equity*MAX_POSITION_PCT).
        size_usd = calculate_position_size(
            account_equity, settled_cash_amount, signal, open_positions, self.params
        )
        if size_usd <= 0:
            # Either concurrency cap hit or no settled cash available.
            return reject("size_zero_concurrency_or_no_settled_cash")

        shares = round(size_usd / signal.current_price, 6) if signal.current_price else 0.0

        rationale = self._rationale(signal, size_usd, len(open_positions))
        proposal = TradeProposal(
            symbol=signal.symbol, direction=LONG, entry_price=signal.current_price,
            size_usd=size_usd, shares=shares, stop_loss_pct=HARD_STOP_LOSS_PCT,
            take_profit_pct=self.params.take_profit_pct,
            aggregated_confidence=signal.calibrated_confidence, atr_pct=atr_pct,
            market_regime=signal.market_regime, active_model=signal.active_model,
            decided_under_failover=signal.decided_under_failover, viable=True,
            reason="ok", contributing=signal.contributing, rationale=rationale,
        )
        log_decision(
            _log, "proposal_built", symbol=signal.symbol, size_usd=size_usd, shares=shares,
            confidence=signal.calibrated_confidence,
        )
        publish_agent_status(AGENT_KEY, "idle", f"{signal.symbol} size ${size_usd:.2f}",
                             active_model=signal.active_model)
        publish_signal_flow("portfolio_construction", signal.symbol, f"size ${size_usd:.2f}")
        return proposal

    def _rationale(self, signal: AggregatedSignal, size_usd: float, n_open: int) -> str:
        """Non-authoritative construction narrative via Fable 5 (offline deterministic)."""
        offline = {
            "rationale": (
                f"Long {signal.symbol} sized ${size_usd:.2f} at conf "
                f"{signal.calibrated_confidence:.2f} with {n_open} open positions; "
                f"volatility- and confidence-scaled per Section 1."
            )
        }
        system = (
            "You explain a position-sizing decision that has ALREADY been computed by code. "
            'Do not change any number. Respond ONLY with JSON: {"rationale": "..."}.'
        )
        user = (
            f"Symbol {signal.symbol}, size ${size_usd:.2f}, confidence "
            f"{signal.calibrated_confidence:.2f}, ATR {signal.atr_14}, {n_open} open positions."
        )
        result = self.client.complete_json(  # type: ignore[attr-defined]
            AGENT_KEY, system, user, offline, agent_name=AGENT_KEY
        )
        return str(result.data.get("rationale", offline["rationale"]))
