"""Agent 4 — Volatility and Options Structure Specialist (spec Section 6).

Interprets volatility conditions and proposes allowed expiration and delta
*bands* — never an order, never a specific contract.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from src.agents.runtime import AgentCallResult, AgentRuntime
from src.agents.schemas import VolatilityAssessment
from src.domain.values import (
    DomainValidationError,
    require_money,
    require_positive_money,
    require_symbol,
    require_utc,
)

AGENT_KEY = "options_specialist"
PROMPT_VERSION = "options_specialist/v1"

SYSTEM_PROMPT = (
    "You are the Volatility and Options Structure Specialist for a defined-risk "
    "options system. You interpret ONLY the deterministic volatility features "
    "provided (IV vs realized, term structure, skew, expected move, event flags). "
    "You propose allowed DTE and delta bands — never an order, contract, or "
    "position size. Respond with a single JSON object matching the "
    "VolatilityAssessment schema; no prose outside the JSON."
)

_SHAPES = frozenset({"contango", "backwardation", "flat"})


@dataclass(frozen=True)
class VolatilityFeaturePacket:
    """Validated deterministic volatility features (Phase C volatility output)."""

    as_of: datetime
    underlying: str
    iv: Decimal
    realized_vol: Decimal
    iv_realized_spread: Decimal
    term_structure_shape: str
    put_call_skew: Decimal
    expected_move_1sd: Decimal
    earnings_before_expiration: bool
    snapshot_ids: tuple[uuid.UUID, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", require_utc("as_of", self.as_of))
        object.__setattr__(self, "underlying", require_symbol("underlying", self.underlying))
        require_positive_money("iv", self.iv)
        realized = require_money("realized_vol", self.realized_vol)
        if realized < 0:
            raise DomainValidationError("realized_vol must be >= 0")
        require_money("iv_realized_spread", self.iv_realized_spread)
        if self.term_structure_shape not in _SHAPES:
            raise DomainValidationError(f"term_structure_shape must be one of {sorted(_SHAPES)}")
        require_money("put_call_skew", self.put_call_skew)
        require_positive_money("expected_move_1sd", self.expected_move_1sd)
        if not isinstance(self.earnings_before_expiration, bool):
            raise DomainValidationError("earnings_before_expiration must be a bool")
        if not self.snapshot_ids:
            raise DomainValidationError("snapshot_ids must not be empty")


def build_user_prompt(packet: VolatilityFeaturePacket) -> str:
    return (
        "Deterministic volatility features (validated, point-in-time):\n"
        f"- underlying: {packet.underlying}\n"
        f"- as_of: {packet.as_of.isoformat()}\n"
        f"- implied_vol: {packet.iv}\n"
        f"- realized_vol: {packet.realized_vol}\n"
        f"- iv_minus_realized: {packet.iv_realized_spread}\n"
        f"- term_structure: {packet.term_structure_shape}\n"
        f"- put_call_skew: {packet.put_call_skew}\n"
        f"- expected_move_1sd: {packet.expected_move_1sd}\n"
        f"- earnings_before_expiration: {packet.earnings_before_expiration}\n"
        "Propose DTE and delta bands per the schema."
    )


def offline_payload(packet: VolatilityFeaturePacket) -> dict[str, Any]:
    """Deterministic band proposal for hermetic runs."""
    if packet.iv_realized_spread >= Decimal("0.05"):
        richness = "rich"
    elif packet.iv_realized_spread <= Decimal("-0.03"):
        richness = "cheap"
    else:
        richness = "fair"

    if packet.earnings_before_expiration:
        event_risk = "high"
    elif packet.term_structure_shape == "backwardation":
        event_risk = "medium"
    else:
        event_risk = "low"

    if richness == "rich":
        delta_min, delta_max = "0.25", "0.45"
    else:
        delta_min, delta_max = "0.30", "0.55"

    return {
        "premium_richness": richness,
        "term_structure_view": (
            f"{packet.term_structure_shape} term structure with IV {packet.iv} vs "
            f"realized {packet.realized_vol}"
        ),
        "skew_view": f"put-call skew {packet.put_call_skew}",
        "event_vol_risk": event_risk,
        "recommended_dte_min": 7,
        "recommended_dte_max": 28,
        "recommended_delta_min": delta_min,
        "recommended_delta_max": delta_max,
        "rationale": (
            f"premium {richness} (iv-rv {packet.iv_realized_spread}); expected move "
            f"{packet.expected_move_1sd}; target policy DTE band 7-28"
        ),
    }


@dataclass(frozen=True)
class OptionsSpecialistAgent:
    runtime: AgentRuntime

    def assess_structure(
        self, packet: VolatilityFeaturePacket, *, correlation_id: uuid.UUID
    ) -> AgentCallResult[VolatilityAssessment]:
        return self.runtime.call(
            agent_key=AGENT_KEY,
            prompt_version=PROMPT_VERSION,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=build_user_prompt(packet),
            output_schema=VolatilityAssessment,
            offline_payload=offline_payload(packet),
            input_snapshot_ids=packet.snapshot_ids,
            correlation_id=correlation_id,
        )
