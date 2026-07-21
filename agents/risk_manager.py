"""Agent 5 — Risk Manager (code-first, thin LLM layer).

Capital-at-risk logic is PURE CODE and is the last line of defence: the approve/block
decision depends only on the Section 0 / Section 1.1 checks, never on LLM judgment
(regardless of model tier). Fable 5 is used only to produce a non-authoritative risk
*narrative flag* (routed through ``config.models``) that annotates the decision for the human
— it can never flip an approval to a block or vice-versa.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from agents.portfolio_construction import TradeProposal
from core.event_bus import publish_agent_status, publish_signal_flow
from core.logging_setup import get_logger, log_decision
from core.records import Position
from risk.sizing import (
    Account,
    HaltDecision,
    assert_purchase_is_covered,
    check_portfolio_halt,
    passes_confidence_gate,
    sector_correlation,
)

_log = get_logger("risk_manager")

AGENT_KEY = "risk_manager_flagging"


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    proposal: TradeProposal
    halt: HaltDecision | None
    narrative_flag: str
    checks: dict[str, bool] = field(default_factory=dict)


class RiskManager:
    def __init__(self, client: object, db: object | None = None) -> None:
        self.client = client
        self.db = db

    def evaluate(
        self,
        proposal: TradeProposal,
        account: Account,
        now: datetime,
        account_equity: float,
        high_water_mark: float,
        daily_start_equity: float,
        open_positions: list[Position],
    ) -> RiskDecision:
        publish_agent_status(AGENT_KEY, "running", f"risk check {proposal.symbol}",
                             active_model=None)
        checks: dict[str, bool] = {}

        # 1. Portfolio-level circuit breakers (pure code).
        halt = check_portfolio_halt(account_equity, high_water_mark, daily_start_equity)
        checks["no_portfolio_halt"] = halt is None
        # Both halt types block a NEW entry (drawdown halts everything; daily-loss halts entries).
        if halt is not None:
            return self._decide(proposal, False, f"blocked_{halt.reason}", halt, checks,
                                open_positions)

        # 2. Upstream construction must be viable.
        checks["proposal_viable"] = proposal.viable
        if not proposal.viable:
            return self._decide(proposal, False, proposal.reason, None, checks, open_positions)

        # 3. Confidence gate (defensive re-check of the Section 0 limit).
        conf_ok = passes_confidence_gate(proposal.aggregated_confidence)
        checks["confidence_gate"] = conf_ok
        if not conf_ok:
            return self._decide(proposal, False, "below_min_confidence", None, checks,
                                open_positions)

        # 4. Positive size.
        checks["positive_size"] = proposal.size_usd > 0
        if proposal.size_usd <= 0:
            return self._decide(proposal, False, "non_positive_size", None, checks,
                                open_positions)

        # 5. Section 1.1 settled-cash coverage backstop (GFV/free-riding guard).
        cover = assert_purchase_is_covered(proposal.size_usd, account, now)
        checks["settled_cash_covered"] = cover.allowed
        if not cover.allowed:
            return self._decide(proposal, False, cover.reason or "would_use_unsettled_funds",
                                None, checks, open_positions)

        return self._decide(proposal, True, "approved", None, checks, open_positions)

    def _decide(
        self,
        proposal: TradeProposal,
        approved: bool,
        reason: str,
        halt: HaltDecision | None,
        checks: dict[str, bool],
        open_positions: list[Position],
    ) -> RiskDecision:
        narrative = self._narrative_flag(proposal, approved, reason, open_positions)
        log_decision(
            _log, "risk_decision", symbol=proposal.symbol, approved=approved, reason=reason,
            checks=checks,
        )
        publish_agent_status(
            AGENT_KEY, "idle",
            f"{proposal.symbol} {'APPROVED' if approved else 'BLOCKED'}: {reason}",
            active_model=proposal.active_model,
        )
        publish_signal_flow(
            "risk_manager", proposal.symbol,
            f"{'approved' if approved else 'blocked'}:{reason}",
        )
        return RiskDecision(
            approved=approved, reason=reason, proposal=proposal, halt=halt,
            narrative_flag=narrative, checks=checks,
        )

    def _narrative_flag(
        self, proposal: TradeProposal, approved: bool, reason: str, open_positions: list[Position]
    ) -> str:
        """Non-authoritative human-facing risk note via Fable 5 (offline deterministic).

        This NEVER changes the code decision above; it only annotates it.
        """
        correlated = [
            p.symbol for p in open_positions if sector_correlation(p.symbol, proposal.symbol) > 0.6
        ]
        offline = {
            "flag": (
                f"{'APPROVED' if approved else 'BLOCKED'} {proposal.symbol} ({reason}). "
                + (f"Correlated with open: {correlated}." if correlated else "No correlated open.")
            )
        }
        system = (
            "You annotate a risk decision that is ALREADY final (made by code). You cannot "
            'change it. Respond ONLY with JSON: {"flag": "one-sentence risk note"}.'
        )
        user = (
            f"Decision: {'approved' if approved else 'blocked'} ({reason}) for {proposal.symbol}, "
            f"size ${proposal.size_usd:.2f}. Correlated open positions: {correlated}."
        )
        result = self.client.complete_json(  # type: ignore[attr-defined]
            AGENT_KEY, system, user, offline, agent_name=AGENT_KEY
        )
        return str(result.data.get("flag", offline["flag"]))
