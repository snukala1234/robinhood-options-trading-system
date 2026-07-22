"""Deterministic opportunity score (spec 5.8) — a ranking aid, not proof of edge.

Every component is a normalized Decimal in [0, 1] produced by deterministic
services; weights come from the tunable config (Section 5.8 defaults). The score is
0-100 with ``risk_penalty`` subtracted in final-score points. Two spec components
share a weight by definition: directional/technical edge averages
``directional_edge`` and ``technical_structure``; liquidity/execution averages
``liquidity`` and ``execution_quality``. Every component is stored, never just the
total.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal

from src.config.tunables import DEFAULT_TUNABLES, TunableParams
from src.domain.values import DomainValidationError, require_money

_TWO_DP = Decimal("0.01")


@dataclass(frozen=True)
class ScoreComponents:
    """Normalized component inputs, each in [0, 1]; risk_penalty in score points."""

    directional_edge: Decimal
    gamma_efficiency: Decimal
    theta_efficiency: Decimal
    volatility_fit: Decimal
    liquidity: Decimal
    catalyst_quality: Decimal
    technical_structure: Decimal
    market_regime_fit: Decimal
    portfolio_fit: Decimal
    execution_quality: Decimal
    expected_value_after_costs: Decimal
    risk_penalty: Decimal  # >= 0, subtracted from the 0-100 total

    def __post_init__(self) -> None:
        for f in fields(self):
            value = require_money(f.name, getattr(self, f.name))
            if f.name == "risk_penalty":
                if value < 0:
                    raise DomainValidationError("risk_penalty must be >= 0")
            elif not (Decimal("0") <= value <= Decimal("1")):
                raise DomainValidationError(f"{f.name} must be in [0, 1], got {value}")


@dataclass(frozen=True)
class OpportunityScore:
    """Spec 5.8 score object: all components plus the weighted total (0-100)."""

    components: ScoreComponents
    weighted: dict[str, Decimal]
    total: Decimal


def compute_score(
    components: ScoreComponents, params: TunableParams = DEFAULT_TUNABLES
) -> OpportunityScore:
    """Weighted 0-100 score minus risk penalty, clamped to [0, 100]."""
    c = components
    weights: dict[str, tuple[Decimal, Decimal]] = {
        "directional_technical_edge": (
            Decimal(str(params.weight_directional_edge)),
            (c.directional_edge + c.technical_structure) / 2,
        ),
        "gamma_efficiency": (
            Decimal(str(params.weight_gamma_efficiency)),
            c.gamma_efficiency,
        ),
        "theta_efficiency": (
            Decimal(str(params.weight_theta_efficiency)),
            c.theta_efficiency,
        ),
        "volatility_fit": (Decimal(str(params.weight_volatility_fit)), c.volatility_fit),
        "liquidity_execution": (
            Decimal(str(params.weight_liquidity_execution)),
            (c.liquidity + c.execution_quality) / 2,
        ),
        "catalyst_quality": (
            Decimal(str(params.weight_catalyst_quality)),
            c.catalyst_quality,
        ),
        "market_regime_fit": (
            Decimal(str(params.weight_market_regime_fit)),
            c.market_regime_fit,
        ),
        "portfolio_fit": (Decimal(str(params.weight_portfolio_fit)), c.portfolio_fit),
        "expected_value": (
            Decimal(str(params.weight_expected_value)),
            c.expected_value_after_costs,
        ),
    }
    weighted = {name: (w * value).quantize(_TWO_DP) for name, (w, value) in weights.items()}
    raw_total = sum(weighted.values(), Decimal("0")) - c.risk_penalty
    total = min(max(raw_total, Decimal("0")), Decimal("100")).quantize(_TWO_DP)
    return OpportunityScore(components=components, weighted=weighted, total=total)
