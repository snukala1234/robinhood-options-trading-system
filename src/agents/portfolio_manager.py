"""Agent 6 — Portfolio Manager (spec Section 6).

Ranks a candidate against available risk budget and current exposures. Its
approval only forwards a proposal to the deterministic trade gate — it can never
bypass a budget the gate enforces in code.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from src.agents.runtime import AgentCallResult, AgentRuntime
from src.agents.schemas import PortfolioManagerDecision
from src.config.risk_policy import MAX_CONCURRENT_POSITIONS
from src.domain.values import (
    DomainValidationError,
    require_money,
    require_non_negative_money,
    require_positive_money,
    require_utc,
)

AGENT_KEY = "portfolio_manager"
PROMPT_VERSION = "portfolio_manager/v1"

SYSTEM_PROMPT = (
    "You are the Portfolio Manager for a defined-risk options system. You rank a "
    "candidate against remaining risk budget, settled cash, concurrency limits, "
    "and correlation with existing exposure. Your options are: approve for the "
    "deterministic gate, reduce requested risk, defer, recommend replacing the "
    "weakest position, or hold cash. Cash is an active allocation. You never "
    "compute risk numbers — budgets arrive computed. Respond with a single JSON "
    "object matching the PortfolioManagerDecision schema; no prose outside it."
)

#: Score-point advantage a candidate needs over the weakest open position before
#: replacement is preferred to deferral.
REPLACEMENT_SCORE_MARGIN = Decimal("15")


@dataclass(frozen=True)
class AllocationPacket:
    """Validated candidate economics and portfolio budget state."""

    as_of: datetime
    candidate_id: str
    opportunity_score_total: Decimal
    candidate_max_loss: Decimal
    remaining_per_trade_budget: Decimal
    remaining_portfolio_budget: Decimal
    settled_cash: Decimal
    capital_required: Decimal
    open_position_count: int
    correlation_with_portfolio: Decimal
    weakest_open_score: Decimal | None
    snapshot_ids: tuple[uuid.UUID, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", require_utc("as_of", self.as_of))
        if not isinstance(self.candidate_id, str) or not self.candidate_id:
            raise DomainValidationError("candidate_id must be a non-empty string")
        score = require_money("opportunity_score_total", self.opportunity_score_total)
        if not (Decimal("0") <= score <= Decimal("100")):
            raise DomainValidationError("opportunity_score_total must be in [0, 100]")
        require_positive_money("candidate_max_loss", self.candidate_max_loss)
        require_non_negative_money("remaining_per_trade_budget", self.remaining_per_trade_budget)
        require_non_negative_money("remaining_portfolio_budget", self.remaining_portfolio_budget)
        require_non_negative_money("settled_cash", self.settled_cash)
        require_positive_money("capital_required", self.capital_required)
        if (
            isinstance(self.open_position_count, bool)
            or not isinstance(self.open_position_count, int)
            or self.open_position_count < 0
        ):
            raise DomainValidationError("open_position_count must be an int >= 0")
        corr = require_money("correlation_with_portfolio", self.correlation_with_portfolio)
        if not (Decimal("0") <= corr <= Decimal("1")):
            raise DomainValidationError("correlation_with_portfolio must be in [0, 1]")
        if self.weakest_open_score is not None:
            require_money("weakest_open_score", self.weakest_open_score)
        if not self.snapshot_ids:
            raise DomainValidationError("snapshot_ids must not be empty")


def build_user_prompt(packet: AllocationPacket) -> str:
    return (
        "Candidate and portfolio state (validated):\n"
        f"- candidate: {packet.candidate_id} (score {packet.opportunity_score_total})\n"
        f"- candidate_max_loss: {packet.candidate_max_loss}\n"
        f"- remaining_per_trade_budget: {packet.remaining_per_trade_budget}\n"
        f"- remaining_portfolio_budget: {packet.remaining_portfolio_budget}\n"
        f"- settled_cash: {packet.settled_cash}\n"
        f"- capital_required: {packet.capital_required}\n"
        f"- open_position_count: {packet.open_position_count} "
        f"(max {MAX_CONCURRENT_POSITIONS})\n"
        f"- correlation_with_portfolio: {packet.correlation_with_portfolio}\n"
        f"- weakest_open_score: {packet.weakest_open_score}\n"
        "Allocate per the schema."
    )


def offline_payload(packet: AllocationPacket) -> dict[str, Any]:
    """Deterministic allocation rules for hermetic runs (first match wins)."""
    if packet.candidate_max_loss > packet.remaining_per_trade_budget:
        return {
            "action": "hold_cash",
            "risk_fraction_of_request": "1",
            "rationale": "candidate max loss exceeds remaining per-trade budget",
        }
    if packet.candidate_max_loss > packet.remaining_portfolio_budget:
        return {
            "action": "hold_cash",
            "risk_fraction_of_request": "1",
            "rationale": "candidate max loss exceeds remaining portfolio risk budget",
        }
    if packet.capital_required > packet.settled_cash:
        return {
            "action": "hold_cash",
            "risk_fraction_of_request": "1",
            "rationale": "capital required exceeds settled cash",
        }
    if packet.open_position_count >= MAX_CONCURRENT_POSITIONS:
        if (
            packet.weakest_open_score is not None
            and packet.opportunity_score_total
            >= packet.weakest_open_score + REPLACEMENT_SCORE_MARGIN
        ):
            return {
                "action": "replace_existing",
                "risk_fraction_of_request": "1",
                "replace_position_id": "weakest",
                "rationale": (
                    f"candidate outscores weakest open position by >= "
                    f"{REPLACEMENT_SCORE_MARGIN} points at the concurrency cap"
                ),
            }
        return {
            "action": "defer",
            "risk_fraction_of_request": "1",
            "rationale": "at concurrency cap without a compelling replacement",
        }
    if packet.correlation_with_portfolio > Decimal("0.6"):
        return {
            "action": "reduce_risk",
            "risk_fraction_of_request": "0.5",
            "rationale": "high correlation with existing exposure; half risk requested",
        }
    return {
        "action": "approve_for_gate",
        "risk_fraction_of_request": "1",
        "rationale": "fits budgets, concurrency, and correlation tolerances",
    }


@dataclass(frozen=True)
class PortfolioManagerAgent:
    runtime: AgentRuntime

    def allocate(
        self, packet: AllocationPacket, *, correlation_id: uuid.UUID
    ) -> AgentCallResult[PortfolioManagerDecision]:
        return self.runtime.call(
            agent_key=AGENT_KEY,
            prompt_version=PROMPT_VERSION,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=build_user_prompt(packet),
            output_schema=PortfolioManagerDecision,
            offline_payload=offline_payload(packet),
            input_snapshot_ids=packet.snapshot_ids,
            correlation_id=correlation_id,
        )
