"""Agent 5 — Strategy Selection Specialist (spec Section 6).

Selects among broker-supported, policy-permitted structures, always comparing at
least two feasible alternatives, with "no trade" as a first-class outcome. A
semantic gate after validation rejects any selection the connected account cannot
actually execute — even a schema-valid answer never propagates an unexecutable
strategy.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from src.agents.runtime import AgentCallResult, AgentRuntime, InvalidAgentOutput
from src.agents.schemas import StrategySelection
from src.config.strategy_registry import STRATEGY_REGISTRY
from src.domain.values import (
    DomainValidationError,
    require_money,
    require_symbol,
    require_utc,
)

AGENT_KEY = "strategy_selector"
PROMPT_VERSION = "strategy_selector/v1"

SYSTEM_PROMPT = (
    "You are the Strategy Selection Specialist for a defined-risk options system. "
    "You select among ONLY the strategies listed as executable for this account, "
    "restricted to the permitted strategy families, and you must compare at least "
    "two alternatives. Selecting no trade is a valid and respectable outcome. You "
    "never size positions or compute risk numbers. Respond with a single JSON "
    "object matching the StrategySelection schema; no prose outside the JSON."
)

_DIRECTIONS = frozenset({"bullish", "bearish", "neutral"})
_RICHNESS = frozenset({"rich", "fair", "cheap"})
_FAMILIES = frozenset({"long_premium", "debit_spreads", "credit_spreads"})


@dataclass(frozen=True)
class StrategySelectionPacket:
    """Validated upstream conclusions plus the account's executable strategy set."""

    as_of: datetime
    underlying: str
    direction: str
    conviction: Decimal
    premium_richness: str
    permitted_families: tuple[str, ...]
    executable_strategies: frozenset[str]
    snapshot_ids: tuple[uuid.UUID, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", require_utc("as_of", self.as_of))
        object.__setattr__(self, "underlying", require_symbol("underlying", self.underlying))
        if self.direction not in _DIRECTIONS:
            raise DomainValidationError(f"direction must be one of {sorted(_DIRECTIONS)}")
        conviction = require_money("conviction", self.conviction)
        if not (Decimal("0") <= conviction <= Decimal("1")):
            raise DomainValidationError("conviction must be in [0, 1]")
        if self.premium_richness not in _RICHNESS:
            raise DomainValidationError(f"premium_richness must be one of {sorted(_RICHNESS)}")
        for family in self.permitted_families:
            if family not in _FAMILIES:
                raise DomainValidationError(f"unknown strategy family {family!r}")
        for strategy in self.executable_strategies:
            if strategy not in STRATEGY_REGISTRY:
                raise DomainValidationError(f"unknown executable strategy {strategy!r}")
        if not self.snapshot_ids:
            raise DomainValidationError("snapshot_ids must not be empty")


def build_user_prompt(packet: StrategySelectionPacket) -> str:
    return (
        "Upstream conclusions (validated):\n"
        f"- underlying: {packet.underlying}\n"
        f"- direction: {packet.direction} (conviction {packet.conviction})\n"
        f"- premium_richness: {packet.premium_richness}\n"
        f"- permitted_families: {sorted(packet.permitted_families)}\n"
        f"- executable_strategies (account capability-gated): "
        f"{sorted(packet.executable_strategies)}\n"
        "Select a strategy (or no trade) per the schema, comparing alternatives."
    )


def _candidates(packet: StrategySelectionPacket) -> list[str]:
    """Preference-ordered feasible structures for the direction (spec 6, Agent 5)."""
    out: list[str] = []
    if packet.direction == "bullish":
        if "debit_spreads" in packet.permitted_families:
            out.append("bull_call_debit_spread")
        if "long_premium" in packet.permitted_families and packet.premium_richness != "rich":
            out.append("long_call")
        if "credit_spreads" in packet.permitted_families:
            out.append("put_credit_spread")
    elif packet.direction == "bearish":
        if "debit_spreads" in packet.permitted_families:
            out.append("bear_put_debit_spread")
        if "long_premium" in packet.permitted_families and packet.premium_richness != "rich":
            out.append("long_put")
        if "credit_spreads" in packet.permitted_families:
            out.append("call_credit_spread")
    return out


def offline_payload(packet: StrategySelectionPacket) -> dict[str, Any]:
    """Deterministic selection for hermetic runs."""
    candidates = _candidates(packet)
    selected: str | None = None
    for candidate in candidates:
        if candidate in packet.executable_strategies:
            selected = candidate
            break

    alternatives: list[dict[str, str]] = []
    for candidate in candidates:
        if candidate == selected:
            continue
        if candidate not in packet.executable_strategies:
            reason = "not executable on this account"
        elif selected is not None:
            reason = "lower expected value than selected structure"
        else:
            reason = "premium too rich for long options"
        alternatives.append({"strategy": candidate, "reason_rejected": reason})
    if not alternatives:
        alternatives.append(
            {
                "strategy": "long_call",
                "reason_rejected": "no strategy family permitted for this regime/direction",
            }
        )

    if selected is None:
        rationale = "no executable strategy fits the permitted families; holding cash"
    else:
        rationale = (
            f"{selected} best expresses a {packet.direction} view with "
            f"{packet.premium_richness} premium under defined risk"
        )
    return {
        "selected_strategy": selected,
        "alternatives_considered": alternatives,
        "rationale": rationale,
    }


@dataclass(frozen=True)
class StrategySelectorAgent:
    runtime: AgentRuntime

    def select(
        self, packet: StrategySelectionPacket, *, correlation_id: uuid.UUID
    ) -> AgentCallResult[StrategySelection]:
        result = self.runtime.call(
            agent_key=AGENT_KEY,
            prompt_version=PROMPT_VERSION,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=build_user_prompt(packet),
            output_schema=StrategySelection,
            offline_payload=offline_payload(packet),
            input_snapshot_ids=packet.snapshot_ids,
            correlation_id=correlation_id,
        )
        selected = result.output.selected_strategy
        if selected is not None and selected not in packet.executable_strategies:
            # Schema-valid but unexecutable: fail closed, never propagate.
            raise InvalidAgentOutput(
                f"selected strategy {selected!r} is not executable on this account"
            )
        return result
