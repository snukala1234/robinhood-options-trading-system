"""Agent 7 — Independent Risk Officer (spec Section 6).

Reviews assumptions, contradiction, event risk, liquidity, and crowding. It has
veto authority at the reasoning layer, but its approval NEVER overrides the
deterministic trade gate — a pass here is necessary, not sufficient, and it
cannot relax any code limit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from src.agents.runtime import AgentCallResult, AgentRuntime
from src.agents.schemas import RiskOfficerDecision
from src.config.strategy_registry import STRATEGY_REGISTRY
from src.domain.values import (
    DomainValidationError,
    require_money,
    require_positive_money,
    require_symbol,
    require_utc,
)

AGENT_KEY = "risk_officer"
PROMPT_VERSION = "risk_officer/v1"

SYSTEM_PROMPT = (
    "You are the Independent Risk Officer for a defined-risk options system. You "
    "review a proposal's risk posture: breached limits, liquidity failures, event "
    "exposure, crowding, and conviction. You may approve, approve with reduction, "
    "or veto. Your approval never overrides the deterministic trade gate and you "
    "cannot relax any code-enforced limit; your veto, however, is final at the "
    "reasoning layer. Respond with a single JSON object matching the "
    "RiskOfficerDecision schema; no prose outside the JSON."
)

_EVENT_RISK = frozenset({"low", "medium", "high"})


@dataclass(frozen=True)
class RiskReviewPacket:
    """Validated proposal risk summary (all numbers computed upstream)."""

    as_of: datetime
    underlying: str
    strategy: str
    max_loss: Decimal
    breached_limit_names: tuple[str, ...]
    liquidity_failures: tuple[str, ...]
    earnings_before_expiration: bool
    event_risk: str
    correlation_with_portfolio: Decimal
    thesis_conviction: Decimal
    snapshot_ids: tuple[uuid.UUID, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", require_utc("as_of", self.as_of))
        object.__setattr__(self, "underlying", require_symbol("underlying", self.underlying))
        if self.strategy not in STRATEGY_REGISTRY:
            raise DomainValidationError(f"unknown strategy {self.strategy!r}")
        require_positive_money("max_loss", self.max_loss)
        if not isinstance(self.earnings_before_expiration, bool):
            raise DomainValidationError("earnings_before_expiration must be a bool")
        if self.event_risk not in _EVENT_RISK:
            raise DomainValidationError(f"event_risk must be one of {sorted(_EVENT_RISK)}")
        corr = require_money("correlation_with_portfolio", self.correlation_with_portfolio)
        if not (Decimal("0") <= corr <= Decimal("1")):
            raise DomainValidationError("correlation_with_portfolio must be in [0, 1]")
        conviction = require_money("thesis_conviction", self.thesis_conviction)
        if not (Decimal("0") <= conviction <= Decimal("1")):
            raise DomainValidationError("thesis_conviction must be in [0, 1]")
        if not self.snapshot_ids:
            raise DomainValidationError("snapshot_ids must not be empty")


def build_user_prompt(packet: RiskReviewPacket) -> str:
    return (
        "Proposal risk summary (validated, all numbers computed by services):\n"
        f"- underlying/strategy: {packet.underlying} {packet.strategy}\n"
        f"- max_loss: {packet.max_loss}\n"
        f"- breached_limit_names: {list(packet.breached_limit_names)}\n"
        f"- liquidity_failures: {list(packet.liquidity_failures)}\n"
        f"- earnings_before_expiration: {packet.earnings_before_expiration}\n"
        f"- event_risk: {packet.event_risk}\n"
        f"- correlation_with_portfolio: {packet.correlation_with_portfolio}\n"
        f"- thesis_conviction: {packet.thesis_conviction}\n"
        "Review per the schema."
    )


def offline_payload(packet: RiskReviewPacket) -> dict[str, Any]:
    """Deterministic review rules for hermetic runs (first match wins)."""
    if packet.breached_limit_names:
        return {
            "decision": "veto",
            "reasons": [f"limit breached: {name}" for name in packet.breached_limit_names],
        }
    if packet.earnings_before_expiration:
        return {
            "decision": "veto",
            "reasons": ["earnings inside holding window (ALLOW_EARNINGS_HOLD is False)"],
        }
    if packet.liquidity_failures:
        return {"decision": "veto", "reasons": list(packet.liquidity_failures)}
    if packet.event_risk == "high" and packet.thesis_conviction < Decimal("0.7"):
        return {
            "decision": "approve_with_reduction",
            "reasons": ["high event risk with moderate conviction"],
            "reduction_fraction": "0.5",
        }
    if packet.correlation_with_portfolio > Decimal("0.6"):
        return {
            "decision": "approve_with_reduction",
            "reasons": ["crowded exposure vs existing positions"],
            "reduction_fraction": "0.5",
        }
    return {"decision": "approve", "reasons": ["within all reasoned risk tolerances"]}


@dataclass(frozen=True)
class RiskOfficerAgent:
    runtime: AgentRuntime

    def review(
        self, packet: RiskReviewPacket, *, correlation_id: uuid.UUID
    ) -> AgentCallResult[RiskOfficerDecision]:
        return self.runtime.call(
            agent_key=AGENT_KEY,
            prompt_version=PROMPT_VERSION,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=build_user_prompt(packet),
            output_schema=RiskOfficerDecision,
            offline_payload=offline_payload(packet),
            input_snapshot_ids=packet.snapshot_ids,
            correlation_id=correlation_id,
        )
