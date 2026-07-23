"""Agent 3 — Technical Structure Analyst (spec Section 6).

Interprets deterministic technical features into a directional thesis with an
explicit invalidation level. Its output schema has no contract, strike, or
expiration fields — this agent cannot recommend a contract, only a view on the
underlying.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from src.agents.runtime import AgentCallResult, AgentRuntime
from src.agents.schemas import TechnicalThesis
from src.domain.values import (
    DomainValidationError,
    require_money,
    require_positive_money,
    require_symbol,
    require_utc,
)

AGENT_KEY = "technical_analyst"
PROMPT_VERSION = "technical_analyst/v1"

SYSTEM_PROMPT = (
    "You are the Technical Structure Analyst for a defined-risk options system. "
    "You interpret ONLY the deterministic technical features provided; you never "
    "invent price levels, never compute risk numbers, and never recommend a "
    "contract, strike, or expiration — only a directional thesis on the "
    "underlying with entry zone, invalidation level, and time horizon. Respond "
    "with a single JSON object matching the TechnicalThesis schema; no prose "
    "outside the JSON."
)

_TRENDS = frozenset({"up", "down", "sideways"})


@dataclass(frozen=True)
class TechnicalFeaturePacket:
    """Validated deterministic technical features (Phase C technicals output)."""

    as_of: datetime
    underlying: str
    close: Decimal
    sma_20: Decimal
    sma_50: Decimal
    atr_14: Decimal
    roc_10: Decimal
    recent_high_20: Decimal
    recent_low_20: Decimal
    trend: str
    snapshot_ids: tuple[uuid.UUID, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", require_utc("as_of", self.as_of))
        object.__setattr__(self, "underlying", require_symbol("underlying", self.underlying))
        for name in ("close", "sma_20", "sma_50", "atr_14", "recent_high_20", "recent_low_20"):
            require_positive_money(name, getattr(self, name))
        require_money("roc_10", self.roc_10)
        if self.trend not in _TRENDS:
            raise DomainValidationError(f"trend must be one of {sorted(_TRENDS)}")
        if not self.snapshot_ids:
            raise DomainValidationError("snapshot_ids must not be empty")


def build_user_prompt(packet: TechnicalFeaturePacket) -> str:
    return (
        "Deterministic technical features (validated, point-in-time):\n"
        f"- underlying: {packet.underlying}\n"
        f"- as_of: {packet.as_of.isoformat()}\n"
        f"- close: {packet.close}\n"
        f"- sma_20: {packet.sma_20}\n"
        f"- sma_50: {packet.sma_50}\n"
        f"- atr_14: {packet.atr_14}\n"
        f"- roc_10: {packet.roc_10}\n"
        f"- recent_high_20: {packet.recent_high_20}\n"
        f"- recent_low_20: {packet.recent_low_20}\n"
        f"- trend: {packet.trend}\n"
        "Produce a directional thesis per the schema."
    )


def _positive_or_close(level: Decimal, close: Decimal) -> str:
    """Schema requires non-negative levels; a degenerate ATR band falls back to close."""
    return str(level) if level > 0 else str(close)


def offline_payload(packet: TechnicalFeaturePacket) -> dict[str, Any]:
    """Deterministic rule-based thesis for hermetic runs."""
    if packet.trend == "up" and packet.roc_10 > 0:
        direction = "bullish"
        conviction = 0.65
        low = _positive_or_close(packet.close - packet.atr_14, packet.close)
        high = str(packet.close)
        invalidation = str(packet.recent_low_20)
        horizon = 14
        path = (
            f"uptrend continuation above sma_20 {packet.sma_20}; pullback entries "
            f"within one ATR of close {packet.close}"
        )
        alternative = f"loss of {packet.recent_low_20} ends the uptrend structure"
    elif packet.trend == "down" and packet.roc_10 < 0:
        direction = "bearish"
        conviction = 0.65
        low = str(packet.close)
        high = str(packet.close + packet.atr_14)
        invalidation = str(packet.recent_high_20)
        horizon = 14
        path = (
            f"downtrend continuation below sma_20 {packet.sma_20}; bounce entries "
            f"within one ATR of close {packet.close}"
        )
        alternative = f"reclaim of {packet.recent_high_20} ends the downtrend structure"
    else:
        direction = "neutral"
        conviction = 0.3
        low = _positive_or_close(packet.close - packet.atr_14, packet.close)
        high = str(packet.close + packet.atr_14)
        invalidation = str(packet.recent_low_20)
        horizon = 10
        path = f"range-bound between {packet.recent_low_20} and {packet.recent_high_20}"
        alternative = "a close outside the 20-bar range would establish a trend"
    return {
        "direction": direction,
        "conviction": conviction,
        "entry_zone_low": low,
        "entry_zone_high": high,
        "invalidation_level": invalidation,
        "time_horizon_days": horizon,
        "expected_path_summary": path,
        "alternative_scenario": alternative,
    }


@dataclass(frozen=True)
class TechnicalAnalystAgent:
    runtime: AgentRuntime

    def analyze(
        self, packet: TechnicalFeaturePacket, *, correlation_id: uuid.UUID
    ) -> AgentCallResult[TechnicalThesis]:
        return self.runtime.call(
            agent_key=AGENT_KEY,
            prompt_version=PROMPT_VERSION,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=build_user_prompt(packet),
            output_schema=TechnicalThesis,
            offline_payload=offline_payload(packet),
            input_snapshot_ids=packet.snapshot_ids,
            correlation_id=correlation_id,
        )
