"""Agent 8 — Position Management Analyst (spec Section 6).

Reviews open positions with fresh data: is the remaining opportunity worth the
remaining decay, volatility, event, and execution risk? Hard stops, drawdown
halts, and DTE/expiration emergency exits are PURE CODE and run regardless of
this agent — its recommendations are discretionary only, and its unavailability
never blocks an exit (it is deliberately not in REQUIRED_ENTRY_AGENTS either).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from src.agents.runtime import AgentCallResult, AgentRuntime
from src.agents.schemas import PositionManagementRecommendation
from src.config.strategy_registry import STRATEGY_REGISTRY
from src.config.tunables import DEFAULT_TUNABLES
from src.domain.values import (
    DomainValidationError,
    require_money,
    require_symbol,
    require_utc,
)

AGENT_KEY = "position_manager"
PROMPT_VERSION = "position_manager/v1"

SYSTEM_PROMPT = (
    "You are the Position Management Analyst for a defined-risk options system. "
    "You review one open position and recommend hold, reduce, take_profit, exit, "
    "roll, or hedge. Hard stops and deterministic emergency exits are pure code "
    "and execute regardless of you; your recommendation is discretionary color on "
    "top, never a replacement for them. You never compute risk numbers. Respond "
    "with a single JSON object matching the PositionManagementRecommendation "
    "schema; no prose outside the JSON."
)


@dataclass(frozen=True)
class PositionReviewPacket:
    """Validated open-position state (all numbers computed upstream)."""

    as_of: datetime
    underlying: str
    strategy: str
    dte: int
    unrealized_pnl_fraction_of_max_gain: Decimal
    unrealized_pnl_fraction_of_max_loss: Decimal
    thesis_intact: bool
    iv_change_since_entry: Decimal
    event_before_expiration: bool
    snapshot_ids: tuple[uuid.UUID, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", require_utc("as_of", self.as_of))
        object.__setattr__(self, "underlying", require_symbol("underlying", self.underlying))
        if self.strategy not in STRATEGY_REGISTRY:
            raise DomainValidationError(f"unknown strategy {self.strategy!r}")
        if isinstance(self.dte, bool) or not isinstance(self.dte, int) or self.dte < 0:
            raise DomainValidationError(f"dte must be an int >= 0, got {self.dte!r}")
        gain_frac = require_money(
            "unrealized_pnl_fraction_of_max_gain", self.unrealized_pnl_fraction_of_max_gain
        )
        if gain_frac > Decimal("1"):
            raise DomainValidationError("gain fraction cannot exceed 1")
        loss_frac = require_money(
            "unrealized_pnl_fraction_of_max_loss", self.unrealized_pnl_fraction_of_max_loss
        )
        if loss_frac > Decimal("1"):
            raise DomainValidationError("loss fraction cannot exceed 1")
        if not isinstance(self.thesis_intact, bool):
            raise DomainValidationError("thesis_intact must be a bool")
        require_money("iv_change_since_entry", self.iv_change_since_entry)
        if not isinstance(self.event_before_expiration, bool):
            raise DomainValidationError("event_before_expiration must be a bool")
        if not self.snapshot_ids:
            raise DomainValidationError("snapshot_ids must not be empty")


def build_user_prompt(packet: PositionReviewPacket) -> str:
    return (
        "Open position state (validated, computed by services):\n"
        f"- underlying/strategy: {packet.underlying} {packet.strategy}\n"
        f"- dte: {packet.dte} (review checkpoint "
        f"{DEFAULT_TUNABLES.dte_review_checkpoint}, forced exit "
        f"{DEFAULT_TUNABLES.dte_forced_exit})\n"
        f"- pnl_fraction_of_max_gain: {packet.unrealized_pnl_fraction_of_max_gain}\n"
        f"- pnl_fraction_of_max_loss: {packet.unrealized_pnl_fraction_of_max_loss}\n"
        f"- thesis_intact: {packet.thesis_intact}\n"
        f"- iv_change_since_entry: {packet.iv_change_since_entry}\n"
        f"- event_before_expiration: {packet.event_before_expiration}\n"
        "Recommend per the schema."
    )


def offline_payload(packet: PositionReviewPacket) -> dict[str, Any]:
    """Deterministic recommendation rules for hermetic runs (first match wins)."""
    if packet.dte <= DEFAULT_TUNABLES.dte_forced_exit:
        return {
            "action": "exit",
            "urgency": "high",
            "rationale": "at forced DTE exit threshold",
            "conditions": [],
        }
    if not packet.thesis_intact:
        return {
            "action": "exit",
            "urgency": "high",
            "rationale": "thesis invalidated; remaining decay is uncompensated",
            "conditions": [],
        }
    if packet.unrealized_pnl_fraction_of_max_gain >= Decimal("0.5"):
        return {
            "action": "take_profit",
            "urgency": "medium",
            "rationale": "profit target region reached; remaining edge vs decay is thin",
            "conditions": ["would hold longer only with fresh momentum confirmation"],
        }
    if packet.event_before_expiration:
        return {
            "action": "exit",
            "urgency": "medium",
            "rationale": "exit before catalyst (no earnings hold)",
            "conditions": [],
        }
    if packet.dte <= DEFAULT_TUNABLES.dte_review_checkpoint:
        return {
            "action": "reduce",
            "urgency": "medium",
            "rationale": "inside DTE review checkpoint; theta acceleration ahead",
            "conditions": ["full exit if no favorable move before forced-exit DTE"],
        }
    return {
        "action": "hold",
        "urgency": "low",
        "rationale": "thesis intact, decay tolerable, no imminent event",
        "conditions": [
            "exit on thesis invalidation",
            "take profit at half of max gain",
        ],
    }


@dataclass(frozen=True)
class PositionManagerAgent:
    runtime: AgentRuntime

    def evaluate_position(
        self, packet: PositionReviewPacket, *, correlation_id: uuid.UUID
    ) -> AgentCallResult[PositionManagementRecommendation]:
        return self.runtime.call(
            agent_key=AGENT_KEY,
            prompt_version=PROMPT_VERSION,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=build_user_prompt(packet),
            output_schema=PositionManagementRecommendation,
            offline_payload=offline_payload(packet),
            input_snapshot_ids=packet.snapshot_ids,
            correlation_id=correlation_id,
        )
