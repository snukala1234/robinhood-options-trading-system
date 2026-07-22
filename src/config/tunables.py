"""Tunable strategy parameters — the ONLY knobs Agent 9 may propose adapting.

Options-native successor to V1's ``config/strategy.py``. Distinct from the hard
guardrails in :mod:`src.config.risk_policy`; a test asserts the two name sets never
overlap, so adaptation can structurally never touch a guardrail. A full snapshot of
these values is what gets stored (immutably) in ``strategy_config_versions.parameters``.

Score weights follow the Section 5.8 conceptual weighting for initial paper testing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class TunableParams:
    """Adaptable parameters: opportunity-score weights and exit-plan thresholds."""

    # Section 5.8 opportunity-score weights (initial paper-testing values).
    weight_directional_edge: float = 15.0
    weight_gamma_efficiency: float = 12.0
    weight_theta_efficiency: float = 12.0
    weight_volatility_fit: float = 10.0
    weight_liquidity_execution: float = 15.0
    weight_catalyst_quality: float = 8.0
    weight_market_regime_fit: float = 8.0
    weight_portfolio_fit: float = 10.0
    weight_expected_value: float = 10.0

    # Exit-plan defaults (Section 10). Profit target as a fraction of max gain;
    # loss exit as a fraction of max loss (tighter than the hard 100% by definition).
    profit_target_pct_of_max_gain: float = 0.50
    loss_exit_pct_of_max_loss: float = 0.50
    # Mandatory DTE review checkpoints (Section 7.2) and default close-before-expiry.
    dte_review_checkpoint: int = 5
    dte_forced_exit: int = 2

    def clamp_to_ranges(self) -> TunableParams:
        """Return a copy with every field clamped into its pre-approved range."""
        return TunableParams(
            weight_directional_edge=_clip(self.weight_directional_edge, 5.0, 25.0),
            weight_gamma_efficiency=_clip(self.weight_gamma_efficiency, 5.0, 20.0),
            weight_theta_efficiency=_clip(self.weight_theta_efficiency, 5.0, 20.0),
            weight_volatility_fit=_clip(self.weight_volatility_fit, 5.0, 20.0),
            weight_liquidity_execution=_clip(self.weight_liquidity_execution, 5.0, 25.0),
            weight_catalyst_quality=_clip(self.weight_catalyst_quality, 0.0, 15.0),
            weight_market_regime_fit=_clip(self.weight_market_regime_fit, 0.0, 15.0),
            weight_portfolio_fit=_clip(self.weight_portfolio_fit, 5.0, 20.0),
            weight_expected_value=_clip(self.weight_expected_value, 5.0, 20.0),
            profit_target_pct_of_max_gain=_clip(self.profit_target_pct_of_max_gain, 0.25, 0.75),
            loss_exit_pct_of_max_loss=_clip(self.loss_exit_pct_of_max_loss, 0.25, 0.75),
            dte_review_checkpoint=int(_clip(self.dte_review_checkpoint, 3, 10)),
            dte_forced_exit=int(_clip(self.dte_forced_exit, 1, 4)),
        )

    def to_dict(self) -> dict[str, float]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TunableParams:
        fields = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in fields})


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


DEFAULT_TUNABLES = TunableParams()

#: Names of all tunable fields — tested against risk_policy.GUARDRAIL_NAMES overlap.
TUNABLE_NAMES = frozenset(TunableParams.__dataclass_fields__)
