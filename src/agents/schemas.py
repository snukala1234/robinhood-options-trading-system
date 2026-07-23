"""Strict output contracts for all nine reasoning agents (spec Section 6).

Every schema forbids extra fields; enums are closed; money-like values travel as
decimal strings (validated parseable) because agents must never emit floats for
money. Cross-field requirements are model validators, so an output that passes
validation is internally coherent — anything else fails closed in the runtime.

Two structural safety validators live here rather than in prompts:
- ``StrategySelection``/alternatives only accept strategies present in the
  Section 7.1 registry.
- ``TunableProposal.parameter`` only accepts names in ``TUNABLE_NAMES`` and
  explicitly rejects guardrail names — Agent 9 can propose tunables, never policy.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.config.risk_policy import GUARDRAIL_NAMES
from src.config.strategy_registry import STRATEGY_REGISTRY
from src.config.tunables import TUNABLE_NAMES


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _require_decimal_string(value: str, *, lo: str | None = None, hi: str | None = None) -> str:
    try:
        dec = Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"not a decimal string: {value!r}") from exc
    if not dec.is_finite():
        raise ValueError(f"not finite: {value!r}")
    if lo is not None and dec < Decimal(lo):
        raise ValueError(f"{value} < {lo}")
    if hi is not None and dec > Decimal(hi):
        raise ValueError(f"{value} > {hi}")
    return value


RiskLevel = Literal["low", "medium", "high"]


# --- Agent 1: Market Regime Strategist --------------------------------------

Regime = Literal[
    "trending_bullish",
    "trending_bearish",
    "range_bound",
    "high_volatility_expansion",
    "volatility_compression",
    "breakout_prone",
    "mean_reverting",
    "event_dominated",
    "risk_off_dislocated",
]

StrategyFamily = Literal["long_premium", "debit_spreads", "credit_spreads"]


class MarketRegimeAssessment(StrictModel):
    regime: Regime
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_features: list[str] = Field(min_length=1, max_length=10)
    contradictory_evidence: list[str] = Field(max_length=10)
    permitted_strategy_families: list[StrategyFamily] = Field(max_length=3)
    avoid_strategy_families: list[StrategyFamily] = Field(max_length=3)

    @model_validator(mode="after")
    def _families_disjoint(self) -> MarketRegimeAssessment:
        overlap = set(self.permitted_strategy_families) & set(self.avoid_strategy_families)
        if overlap:
            raise ValueError(f"strategy families both permitted and avoided: {overlap}")
        return self


# --- Agent 2: Universe and Catalyst Researcher -------------------------------

CatalystType = Literal[
    "earnings",
    "product",
    "regulatory",
    "litigation",
    "macro",
    "analyst",
    "corporate_action",
    "sector",
]


class CatalystItem(StrictModel):
    catalyst_type: CatalystType
    description: str = Field(min_length=1, max_length=300)
    scheduled: bool
    expected_date: str | None = None  # ISO date when known
    timing_vs_expiration: Literal["before_expiration", "after_expiration", "unknown"]
    pricing_state: Literal["priced_in", "partially_priced", "not_priced", "stale"]
    gap_risk: RiskLevel
    iv_crush_risk: RiskLevel


class CatalystAssessment(StrictModel):
    catalysts: list[CatalystItem] = Field(max_length=10)
    facts: list[str] = Field(max_length=15)
    interpretations: list[str] = Field(max_length=15)
    overall_event_risk: RiskLevel
    #: Must be True when source text contained directive-like content; the agent
    #: reports injection attempts instead of following them.
    suspicious_content_detected: bool


# --- Agent 3: Technical Structure Analyst ------------------------------------


class TechnicalThesis(StrictModel):
    """Directional thesis. Deliberately has NO contract/strike/expiration fields —
    this agent cannot recommend a contract (spec Section 6)."""

    direction: Literal["bullish", "bearish", "neutral"]
    conviction: float = Field(ge=0.0, le=1.0)
    entry_zone_low: str
    entry_zone_high: str
    invalidation_level: str
    time_horizon_days: int = Field(ge=1, le=60)
    expected_path_summary: str = Field(min_length=1, max_length=500)
    alternative_scenario: str = Field(min_length=1, max_length=500)

    @field_validator("entry_zone_low", "entry_zone_high", "invalidation_level")
    @classmethod
    def _decimal_strings(cls, v: str) -> str:
        return _require_decimal_string(v, lo="0")

    @model_validator(mode="after")
    def _zone_ordered(self) -> TechnicalThesis:
        if Decimal(self.entry_zone_low) > Decimal(self.entry_zone_high):
            raise ValueError("entry_zone_low > entry_zone_high")
        return self


# --- Agent 4: Volatility and Options Structure Specialist --------------------


class VolatilityAssessment(StrictModel):
    """Proposes expiration/delta bands, never an order (spec Section 6)."""

    premium_richness: Literal["rich", "fair", "cheap"]
    term_structure_view: str = Field(min_length=1, max_length=300)
    skew_view: str = Field(min_length=1, max_length=300)
    event_vol_risk: RiskLevel
    recommended_dte_min: int = Field(ge=5, le=35)
    recommended_dte_max: int = Field(ge=5, le=35)
    recommended_delta_min: str
    recommended_delta_max: str
    rationale: str = Field(min_length=1, max_length=500)

    @field_validator("recommended_delta_min", "recommended_delta_max")
    @classmethod
    def _delta_strings(cls, v: str) -> str:
        return _require_decimal_string(v, lo="0", hi="1")

    @model_validator(mode="after")
    def _bands_ordered(self) -> VolatilityAssessment:
        if self.recommended_dte_min > self.recommended_dte_max:
            raise ValueError("recommended_dte_min > recommended_dte_max")
        if Decimal(self.recommended_delta_min) > Decimal(self.recommended_delta_max):
            raise ValueError("recommended_delta_min > recommended_delta_max")
        return self


# --- Agent 5: Strategy Selection Specialist ----------------------------------


class StrategyAlternative(StrictModel):
    strategy: str
    reason_rejected: str = Field(min_length=1, max_length=300)

    @field_validator("strategy")
    @classmethod
    def _known_strategy(cls, v: str) -> str:
        if v not in STRATEGY_REGISTRY:
            raise ValueError(f"unknown strategy {v!r}")
        return v


class StrategySelection(StrictModel):
    """``selected_strategy=None`` means no trade — an explicitly valid outcome."""

    selected_strategy: str | None
    alternatives_considered: list[StrategyAlternative] = Field(min_length=1, max_length=6)
    rationale: str = Field(min_length=1, max_length=600)

    @field_validator("selected_strategy")
    @classmethod
    def _known_or_none(cls, v: str | None) -> str | None:
        if v is not None and v not in STRATEGY_REGISTRY:
            raise ValueError(f"unknown strategy {v!r}")
        return v


# --- Agent 6: Portfolio Manager ----------------------------------------------


class PortfolioManagerDecision(StrictModel):
    action: Literal["approve_for_gate", "reduce_risk", "defer", "replace_existing", "hold_cash"]
    risk_fraction_of_request: str = "1"
    replace_position_id: str | None = None
    rationale: str = Field(min_length=1, max_length=600)

    @field_validator("risk_fraction_of_request")
    @classmethod
    def _fraction(cls, v: str) -> str:
        return _require_decimal_string(v, lo="0", hi="1")

    @model_validator(mode="after")
    def _coherent(self) -> PortfolioManagerDecision:
        if self.action == "reduce_risk" and Decimal(self.risk_fraction_of_request) >= 1:
            raise ValueError("reduce_risk requires risk_fraction_of_request < 1")
        if self.action == "replace_existing" and not self.replace_position_id:
            raise ValueError("replace_existing requires replace_position_id")
        if self.action != "replace_existing" and self.replace_position_id is not None:
            raise ValueError("replace_position_id only valid with replace_existing")
        return self


# --- Agent 7: Independent Risk Officer ---------------------------------------


class RiskOfficerDecision(StrictModel):
    decision: Literal["approve", "approve_with_reduction", "veto"]
    reasons: list[str] = Field(min_length=1, max_length=10)
    reduction_fraction: str | None = None

    @field_validator("reduction_fraction")
    @classmethod
    def _reduction(cls, v: str | None) -> str | None:
        if v is not None:
            _require_decimal_string(v, lo="0", hi="1")
            if not Decimal("0") < Decimal(v) < Decimal("1"):
                raise ValueError("reduction_fraction must be in (0, 1)")
        return v

    @model_validator(mode="after")
    def _coherent(self) -> RiskOfficerDecision:
        if self.decision == "approve_with_reduction" and self.reduction_fraction is None:
            raise ValueError("approve_with_reduction requires reduction_fraction")
        if self.decision != "approve_with_reduction" and self.reduction_fraction is not None:
            raise ValueError("reduction_fraction only valid with approve_with_reduction")
        return self


# --- Agent 8: Position Management Analyst ------------------------------------


class PositionManagementRecommendation(StrictModel):
    action: Literal["hold", "reduce", "take_profit", "exit", "roll", "hedge"]
    urgency: RiskLevel
    rationale: str = Field(min_length=1, max_length=600)
    conditions: list[str] = Field(max_length=8)


# --- Agent 9: Performance and Calibration Auditor ----------------------------


class TunableProposal(StrictModel):
    parameter: str
    proposed_value: float
    evidence_summary: str = Field(min_length=1, max_length=400)
    sample_size: int = Field(ge=1)

    @field_validator("parameter")
    @classmethod
    def _tunable_only(cls, v: str) -> str:
        if v in GUARDRAIL_NAMES:
            raise ValueError(f"{v!r} is a hard guardrail; agents can never propose it")
        if v not in TUNABLE_NAMES:
            raise ValueError(f"{v!r} is not a pre-approved tunable parameter")
        return v


class CalibrationReport(StrictModel):
    findings: list[str] = Field(max_length=15)
    proposals: list[TunableProposal] = Field(max_length=8)
    hold_reason: str | None = None

    @model_validator(mode="after")
    def _explicit_when_empty(self) -> CalibrationReport:
        if not self.proposals and self.hold_reason is None:
            raise ValueError("no proposals requires an explicit hold_reason")
        return self
