"""Confidence banding + recalibration (shared by research agents and Agent 8).

Section 3.2 buckets closed trades by confidence band; Section 3.3 applies a per-agent,
per-band *recalibration factor* to raw model confidence (e.g. "technical agent's 80% calls
resolve at 65% historically -> apply -15pt correction"). Research agents consume the factor
here; Agent 8 (Component 9) computes and persists it via shadow-tested config versions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

BELOW = "below_0.65"
BAND_065_070 = "0.65-0.70"
BAND_070_080 = "0.70-0.80"
BAND_080_100 = "0.80-1.00"

TRADEABLE_BANDS = (BAND_065_070, BAND_070_080, BAND_080_100)

_MIDPOINTS = {
    BELOW: 0.55,
    BAND_065_070: 0.675,
    BAND_070_080: 0.75,
    BAND_080_100: 0.90,
}


def confidence_band(confidence: float) -> str:
    """Map a confidence to its Section 3.2 band."""
    if confidence >= 0.80:
        return BAND_080_100
    if confidence >= 0.70:
        return BAND_070_080
    if confidence >= 0.65:
        return BAND_065_070
    return BELOW


def band_midpoint(band: str) -> float:
    """Expected hit rate implied by a band (its midpoint)."""
    return _MIDPOINTS[band]


@dataclass
class RecalibrationStore:
    """Per-(agent, band) additive confidence corrections in confidence points.

    A delta of -0.15 means "subtract 15 points from this agent's raw confidence in this band."
    Empty by default (identity) until Agent 8 has a statistically significant sample.
    """

    deltas: dict[tuple[str, str], float] = field(default_factory=dict)

    def get(self, source_agent: str, raw_confidence: float) -> float:
        band = confidence_band(raw_confidence)
        return self.deltas.get((source_agent, band), 0.0)

    @classmethod
    def from_params(cls, params: dict[str, object]) -> RecalibrationStore:
        """Build from a strategy-config ``recalibration`` block: {"agent|band": delta}."""
        raw = params.get("recalibration", {})
        deltas: dict[tuple[str, str], float] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                if "|" in key:
                    agent, band = key.split("|", 1)
                    deltas[(agent, band)] = float(value)
        return cls(deltas=deltas)

    def to_params(self) -> dict[str, float]:
        return {f"{agent}|{band}": delta for (agent, band), delta in self.deltas.items()}


EMPTY_RECALIBRATION = RecalibrationStore()


def apply_recalibration(
    store: RecalibrationStore, source_agent: str, raw_confidence: float
) -> float:
    """Return the calibrated confidence: raw + delta, clamped to [0, 1]."""
    calibrated = raw_confidence + store.get(source_agent, raw_confidence)
    return max(0.0, min(1.0, calibrated))
