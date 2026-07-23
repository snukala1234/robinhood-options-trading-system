"""Agent 1 — Market Regime Strategist (spec Section 6).

Classifies the market environment from validated deterministic features. This
module is also the reference pattern for every other agent in the package:

- ``PROMPT_VERSION``: immutable, logged with every call; any prompt change means
  a NEW version string, never an edit to an existing one.
- A frozen, validated feature packet — the agent's only input. It carries the
  snapshot IDs of the deterministic data it was built from.
- ``offline_payload``: a deterministic, rule-based stand-in used in hermetic mode
  so paper runs and tests need no network. It goes through the same strict schema.
- The agent never computes trusted numbers and has no tools.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from src.agents.runtime import AgentCallResult, AgentRuntime
from src.agents.schemas import MarketRegimeAssessment
from src.domain.values import DomainValidationError, require_money, require_utc

AGENT_KEY = "market_regime"
PROMPT_VERSION = "market_regime/v1"

SYSTEM_PROMPT = (
    "You are the Market Regime Strategist for a defined-risk options system. "
    "You interpret ONLY the deterministic features provided in the user message; "
    "you never invent data, never compute risk numbers, and never recommend "
    "specific trades or contracts. Classify the market regime, state your "
    "confidence, cite supporting features and contradictory evidence, and list "
    "which defined-risk strategy families the regime permits or disfavors. "
    "Respond with a single JSON object matching the MarketRegimeAssessment "
    "schema. No prose outside the JSON."
)

_TRENDS = frozenset({"up", "down", "sideways"})
_SHAPES = frozenset({"contango", "backwardation", "flat"})


@dataclass(frozen=True)
class RegimeFeaturePacket:
    """Validated deterministic features (Phase C outputs) for regime assessment."""

    as_of: datetime
    trend: str  # technicals.TechnicalFeatures.trend
    roc_10: Decimal
    atr_pct: Decimal
    realized_vol: Decimal
    iv_realized_spread: Decimal
    term_structure_shape: str
    put_call_skew: Decimal
    snapshot_ids: tuple[uuid.UUID, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", require_utc("as_of", self.as_of))
        if self.trend not in _TRENDS:
            raise DomainValidationError(f"trend must be one of {sorted(_TRENDS)}")
        if self.term_structure_shape not in _SHAPES:
            raise DomainValidationError(f"term_structure_shape must be one of {sorted(_SHAPES)}")
        for name in ("roc_10", "atr_pct", "realized_vol", "iv_realized_spread", "put_call_skew"):
            require_money(name, getattr(self, name))
        if not self.snapshot_ids:
            raise DomainValidationError("snapshot_ids must not be empty")


def build_user_prompt(packet: RegimeFeaturePacket) -> str:
    return (
        "Deterministic market features (validated, point-in-time):\n"
        f"- as_of: {packet.as_of.isoformat()}\n"
        f"- trend: {packet.trend}\n"
        f"- roc_10: {packet.roc_10}\n"
        f"- atr_pct: {packet.atr_pct}\n"
        f"- realized_vol (annualized): {packet.realized_vol}\n"
        f"- iv_minus_realized: {packet.iv_realized_spread}\n"
        f"- term_structure: {packet.term_structure_shape}\n"
        f"- put_call_skew: {packet.put_call_skew}\n"
        "Classify the regime per the schema."
    )


def offline_payload(packet: RegimeFeaturePacket) -> dict[str, Any]:
    """Deterministic rule-based classification for hermetic runs."""
    high_vol = packet.realized_vol >= Decimal("0.30")
    rich_iv = packet.iv_realized_spread >= Decimal("0.05")
    cheap_iv = packet.iv_realized_spread <= Decimal("-0.03")

    if high_vol and packet.term_structure_shape == "backwardation":
        regime, permitted, avoid = "risk_off_dislocated", [], ["credit_spreads"]
    elif high_vol:
        regime, permitted, avoid = (
            "high_volatility_expansion",
            ["debit_spreads"],
            ["credit_spreads"],
        )
    elif cheap_iv and packet.trend == "sideways":
        regime, permitted, avoid = "volatility_compression", ["long_premium"], []
    elif packet.trend == "up" and packet.roc_10 > 0:
        regime, permitted, avoid = (
            "trending_bullish",
            ["debit_spreads", "long_premium"],
            [],
        )
    elif packet.trend == "down" and packet.roc_10 < 0:
        regime, permitted, avoid = (
            "trending_bearish",
            ["debit_spreads", "long_premium"],
            [],
        )
    elif rich_iv:
        regime, permitted, avoid = "range_bound", ["credit_spreads"], ["long_premium"]
    else:
        regime, permitted, avoid = "mean_reverting", ["debit_spreads"], []

    supporting = [
        f"trend={packet.trend}",
        f"realized_vol={packet.realized_vol}",
        f"iv_minus_realized={packet.iv_realized_spread}",
        f"term_structure={packet.term_structure_shape}",
    ]
    return {
        "regime": regime,
        "confidence": 0.6,
        "supporting_features": supporting,
        "contradictory_evidence": [],
        "permitted_strategy_families": permitted,
        "avoid_strategy_families": avoid,
    }


@dataclass(frozen=True)
class MarketRegimeAgent:
    runtime: AgentRuntime

    def assess(
        self, packet: RegimeFeaturePacket, *, correlation_id: uuid.UUID
    ) -> AgentCallResult[MarketRegimeAssessment]:
        return self.runtime.call(
            agent_key=AGENT_KEY,
            prompt_version=PROMPT_VERSION,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=build_user_prompt(packet),
            output_schema=MarketRegimeAssessment,
            offline_payload=offline_payload(packet),
            input_snapshot_ids=packet.snapshot_ids,
            correlation_id=correlation_id,
        )
