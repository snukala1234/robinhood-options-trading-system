"""Agent 9 — Performance and Calibration Auditor (spec Section 6).

Evaluates calibration buckets and may propose bounded changes to TUNABLE
parameters only. Proposals are recommendations: promotion requires shadow testing
and explicit human approval (Section 13.4). Guardrails are structurally
unproposable — the output schema rejects any guardrail name.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from src.agents.runtime import AgentCallResult, AgentRuntime
from src.agents.schemas import CalibrationReport
from src.domain.values import DomainValidationError, require_money, require_utc

AGENT_KEY = "performance_auditor"
PROMPT_VERSION = "performance_auditor/v1"

SYSTEM_PROMPT = (
    "You are the Performance and Calibration Auditor for a defined-risk options "
    "system. You evaluate calibration buckets (by strategy, DTE, delta, regime, "
    "and so on) and may propose changes ONLY to pre-approved tunable parameters, "
    "only with sufficient sample size, and only as recommendations — every "
    "proposal goes to shadow testing and human review before promotion. A losing "
    "trade is not automatically an error; insufficient evidence means holding. "
    "Respond with a single JSON object matching the CalibrationReport schema; no "
    "prose outside the JSON."
)


@dataclass(frozen=True)
class BucketStat:
    """One calibration bucket's realized statistics (computed upstream)."""

    dimension: str
    sample_size: int
    win_rate: Decimal
    expectancy_after_costs: Decimal
    avg_slippage: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.dimension, str) or not self.dimension:
            raise DomainValidationError("dimension must be a non-empty string")
        if (
            isinstance(self.sample_size, bool)
            or not isinstance(self.sample_size, int)
            or self.sample_size < 0
        ):
            raise DomainValidationError("sample_size must be an int >= 0")
        win_rate = require_money("win_rate", self.win_rate)
        if not (Decimal("0") <= win_rate <= Decimal("1")):
            raise DomainValidationError("win_rate must be in [0, 1]")
        require_money("expectancy_after_costs", self.expectancy_after_costs)
        require_money("avg_slippage", self.avg_slippage)


@dataclass(frozen=True)
class AuditPacket:
    """Validated calibration inputs."""

    as_of: datetime
    buckets: tuple[BucketStat, ...]
    min_sample_size: int
    snapshot_ids: tuple[uuid.UUID, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", require_utc("as_of", self.as_of))
        if (
            isinstance(self.min_sample_size, bool)
            or not isinstance(self.min_sample_size, int)
            or self.min_sample_size < 1
        ):
            raise DomainValidationError("min_sample_size must be an int >= 1")
        if not self.snapshot_ids:
            raise DomainValidationError("snapshot_ids must not be empty")


def build_user_prompt(packet: AuditPacket) -> str:
    lines = [
        f"Calibration buckets as of {packet.as_of.isoformat()} "
        f"(min sample size {packet.min_sample_size}):"
    ]
    if packet.buckets:
        lines.extend(
            f"- {b.dimension}: n={b.sample_size} win_rate={b.win_rate} "
            f"expectancy={b.expectancy_after_costs} slippage={b.avg_slippage}"
            for b in packet.buckets
        )
    else:
        lines.append("- none")
    lines.append("Audit per the schema (tunable parameters only).")
    return "\n".join(lines)


def offline_payload(packet: AuditPacket) -> dict[str, Any]:
    """Deterministic audit for hermetic runs. At most one conservative proposal."""
    qualified = [b for b in packet.buckets if b.sample_size >= packet.min_sample_size]
    findings = [
        f"{b.dimension}: n={b.sample_size} win_rate={b.win_rate} "
        f"expectancy={b.expectancy_after_costs}"
        for b in qualified
    ]
    proposals: list[dict[str, Any]] = []
    for bucket in qualified:
        if bucket.expectancy_after_costs < 0 and bucket.win_rate < Decimal("0.4"):
            proposals.append(
                {
                    "parameter": "profit_target_pct_of_max_gain",
                    "proposed_value": 0.4,
                    "evidence_summary": f"negative expectancy in {bucket.dimension}",
                    "sample_size": bucket.sample_size,
                }
            )
            break  # one conservative proposal at a time
    hold_reason: str | None = None
    if not proposals:
        hold_reason = (
            "no bucket meets evidence threshold"
            if qualified
            else "insufficient sample size in all buckets"
        )
    return {
        "findings": findings,
        "proposals": proposals,
        "hold_reason": hold_reason,
    }


@dataclass(frozen=True)
class PerformanceAuditorAgent:
    runtime: AgentRuntime

    def audit(
        self, packet: AuditPacket, *, correlation_id: uuid.UUID
    ) -> AgentCallResult[CalibrationReport]:
        return self.runtime.call(
            agent_key=AGENT_KEY,
            prompt_version=PROMPT_VERSION,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=build_user_prompt(packet),
            output_schema=CalibrationReport,
            offline_payload=offline_payload(packet),
            input_snapshot_ids=packet.snapshot_ids,
            correlation_id=correlation_id,
        )
