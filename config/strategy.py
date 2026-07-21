"""Tunable strategy parameters — the ONLY knobs Agent 8 may adapt (Section 3.3).

These are distinct from the Section 0 hard guardrails in ``config.guardrails`` and never
overlap them. Defaults reproduce the literal values in the Section 1 sizing pseudocode, so
with default parameters :mod:`risk.sizing` behaves exactly as the spec specifies. Agent 8
proposes changes to these within pre-approved ranges, validated by shadow-testing and a
human checkpoint before promotion; a full snapshot of these values is what gets stored in
``strategy_config_versions.parameters``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class StrategyParams:
    """Full snapshot of adaptable parameters (Section 1 sizing + exit thresholds)."""

    # Volatility-adjusted sizing (Section 1 step 3): scalar = clamp(vol_target/atr_pct, ...)
    vol_target: float = 0.02
    vol_scalar_min: float = 0.4
    vol_scalar_max: float = 1.0

    # Confidence-adjusted sizing (Section 1 step 4).
    conf_scalar_min: float = 0.3
    conf_scalar_max: float = 1.0

    # Sector/correlation de-risking (Section 1 step 5).
    correlation_threshold: float = 0.6
    correlation_penalty: float = 0.5

    # Exit thresholds (Section 3.3, Exit Agent). Take-profit is a strategy param, not a
    # Section 0 guardrail. The stop used at entry defaults to the hard stop and can only be
    # made *tighter* by Agent 8 — never looser than HARD_STOP_LOSS_PCT (enforced in risk).
    take_profit_pct: float = 0.25

    # Pre-approved adaptation ranges (Agent 8 cannot move a param outside its range).
    def clamp_to_ranges(self) -> StrategyParams:
        """Return a copy with every field clamped into its pre-approved range."""
        return StrategyParams(
            vol_target=_clip(self.vol_target, 0.01, 0.04),
            vol_scalar_min=_clip(self.vol_scalar_min, 0.2, 0.6),
            vol_scalar_max=_clip(self.vol_scalar_max, 0.8, 1.0),
            conf_scalar_min=_clip(self.conf_scalar_min, 0.2, 0.5),
            conf_scalar_max=_clip(self.conf_scalar_max, 0.8, 1.0),
            correlation_threshold=_clip(self.correlation_threshold, 0.5, 0.8),
            correlation_penalty=_clip(self.correlation_penalty, 0.3, 0.7),
            take_profit_pct=_clip(self.take_profit_pct, 0.10, 0.40),
        )

    def to_dict(self) -> dict[str, float]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, float]) -> StrategyParams:
        fields = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in fields})


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


DEFAULT_STRATEGY = StrategyParams()
