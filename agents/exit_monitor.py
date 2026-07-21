"""Agent 7 — Position Monitor / Exit Agent.

Stop-loss and take-profit are PURE CODE (Section 1), evaluated first on every poll tick so a
forced exit never depends on an LLM. Thesis-invalidation is a Fable-5 call (genuine
reasoning). Per Section 3.8 the exit monitor has the SHORTEST failover tolerance of any
agent: if it cannot reach any model in the chain, it silently falls back to the hard-coded
stop/take-profit rules alone rather than leaving a position unmonitored.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from core.event_bus import publish_agent_status, publish_signal_flow
from core.llm import AllModelsUnavailableError
from core.logging_setup import get_logger, log_decision
from core.records import Position
from risk.sizing import check_stop_loss_cash_account

_log = get_logger("exit_monitor")

AGENT_KEY = "exit_monitor"

# Exit reasons (Section 3.1 exit taxonomy).
REASON_STOP_LOSS = "stop_loss"
REASON_TAKE_PROFIT = "take_profit"
REASON_THESIS = "thesis_invalidated"
REASON_SCHEDULED = "scheduled_review"

ACTION_HOLD = "hold"
ACTION_EXIT = "exit"


@dataclass
class ExitDecision:
    position: Position
    action: str
    reason: str
    current_price: float
    forced: bool
    thesis_checked: bool
    unrealized_pnl: float


class ExitMonitor:
    def __init__(self, client: object, db: object | None = None, thesis_enabled: bool = True) -> None:
        self.client = client
        self.db = db
        self.thesis_enabled = thesis_enabled

    def evaluate_position(
        self, position: Position, current_price: float, market_regime: str = "normal",
        scheduled_review: bool = False,
    ) -> ExitDecision:
        unrealized = round((current_price - position.entry_price) * position.shares, 2)

        # 1. Forced hard stop-loss (pure code, unconditional on a cash account).
        if check_stop_loss_cash_account(position, current_price) is not None:
            return self._exit(position, REASON_STOP_LOSS, current_price, unrealized,
                              forced=True, thesis_checked=False)

        # 2. Take-profit (pure code).
        gain = (current_price - position.entry_price) / position.entry_price
        if gain >= position.take_profit_pct:
            return self._exit(position, REASON_TAKE_PROFIT, current_price, unrealized,
                              forced=False, thesis_checked=False)

        # 3. Thesis invalidation (Fable 5, shortest failover tolerance).
        thesis_checked = False
        if self.thesis_enabled:
            invalidated, thesis_checked = self._thesis_invalidated(
                position, current_price, market_regime
            )
            if invalidated:
                return self._exit(position, REASON_THESIS, current_price, unrealized,
                                  forced=False, thesis_checked=True)

        # 4. Optional scheduled-review close (e.g. flatten stale names at session review).
        if scheduled_review and gain <= 0:
            return self._exit(position, REASON_SCHEDULED, current_price, unrealized,
                              forced=False, thesis_checked=thesis_checked)

        return ExitDecision(position, ACTION_HOLD, "", current_price, False, thesis_checked,
                            unrealized)

    def evaluate_all(
        self, positions: list[Position], price_of: Callable[[str], float],
        market_regime: str = "normal", scheduled_review: bool = False,
    ) -> list[ExitDecision]:
        """One poll tick over all open positions."""
        publish_agent_status(AGENT_KEY, "running", f"monitoring {len(positions)} positions",
                             active_model=None)
        decisions = [
            self.evaluate_position(p, price_of(p.symbol), market_regime, scheduled_review)
            for p in positions
        ]
        exits = [d for d in decisions if d.action == ACTION_EXIT]
        publish_agent_status(AGENT_KEY, "idle",
                             f"{len(exits)} exits of {len(positions)} positions",
                             active_model=None)
        return decisions

    def _exit(
        self, position: Position, reason: str, price: float, unrealized: float,
        forced: bool, thesis_checked: bool,
    ) -> ExitDecision:
        log_decision(_log, "exit_signal", symbol=position.symbol, reason=reason,
                     forced=forced, price=price, unrealized=unrealized)
        publish_signal_flow("exit_monitor", position.symbol, f"exit:{reason}")
        return ExitDecision(position, ACTION_EXIT, reason, price, forced, thesis_checked,
                            unrealized)

    def _thesis_invalidated(
        self, position: Position, current_price: float, market_regime: str
    ) -> tuple[bool, bool]:
        """Return (invalidated, thesis_checked). Never raises: on total model outage, falls
        back to pure-code stops only (returns (False, False))."""
        offline = {"invalidated": False, "reason": "thesis intact (offline default)"}
        system = (
            "You decide whether new information has invalidated the thesis for holding a long "
            'position. Respond ONLY with JSON: {"invalidated": true|false, "reason": "..."}.'
        )
        gain = (current_price - position.entry_price) / position.entry_price
        user = (
            f"Long {position.symbol} entered at {position.entry_price}, now {current_price} "
            f"({gain:+.1%}). Regime {market_regime}. Has the thesis been invalidated?"
        )
        try:
            result = self.client.complete_json(  # type: ignore[attr-defined]
                AGENT_KEY, system, user, offline, agent_name=AGENT_KEY
            )
        except AllModelsUnavailableError:
            # Shortest failover tolerance: do NOT leave the position unmonitored — the
            # pure-code stop/take-profit rules above have already run. Just skip the thesis.
            log_decision(_log, "thesis_check_skipped_model_outage", symbol=position.symbol)
            return False, False
        return bool(result.data.get("invalidated", False)), True
