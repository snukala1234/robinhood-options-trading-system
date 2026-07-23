"""Section 13.2 calibration buckets — all thirteen options-native dimensions.

Band edges are fixed, documented constants: a record lands in exactly one
bucket per dimension, so bucket membership is reproducible forever from the
stored record. Dollar-exposure bands are normalized by the trade's own
defined max risk so account size never shifts a bucket.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal

from src.learning.records import TradeRecord
from src.orchestration.calendar import ET

D = Decimal


def dte_band(dte: int) -> str:
    if dte <= 7:
        return "0-7"
    if dte <= 14:
        return "8-14"
    if dte <= 21:
        return "15-21"
    return "22+"


def delta_band(net_delta_per_unit: Decimal) -> str:
    magnitude = abs(net_delta_per_unit)
    if magnitude < D("0.25"):
        return "sub_25"
    if magnitude < D("0.45"):
        return "25_45"
    if magnitude < D("0.60"):
        return "45_60"
    return "60_plus"


def _exposure_band(dollars: Decimal, max_risk: Decimal) -> str:
    ratio = abs(dollars) / max_risk
    if ratio < D("0.05"):
        return "low"
    if ratio < D("0.20"):
        return "mid"
    return "high"


def gamma_theta_band(record: TradeRecord) -> str:
    gamma = _exposure_band(record.gamma_dollars, record.max_risk)
    theta = _exposure_band(record.theta_dollars_daily, record.max_risk)
    return f"gamma_{gamma}/theta_{theta}"


def iv_state_band(iv_rank: Decimal, term_structure_state: str) -> str:
    if iv_rank < 25:
        rank = "low"
    elif iv_rank < 50:
        rank = "mid"
    elif iv_rank < 75:
        rank = "elevated"
    else:
        rank = "high"
    return f"{rank}_{term_structure_state}"


def spread_quality_band(spread_pct: Decimal) -> str:
    if spread_pct <= D("0.02"):
        return "tight"
    if spread_pct <= D("0.06"):
        return "normal"
    if spread_pct <= D("0.12"):
        return "wide"
    return "very_wide"


def entry_hour_band(record: TradeRecord) -> str:
    return f"{record.entered_at.astimezone(ET).hour:02d}"


def holding_duration_band(days: int) -> str:
    if days <= 1:
        return "0-1d"
    if days <= 3:
        return "2-3d"
    if days <= 7:
        return "4-7d"
    return "8d+"


def score_band(score: Decimal) -> str:
    if score < 75:
        return "sub_75"
    if score < 80:
        return "75-80"
    if score < 90:
        return "80-90"
    return "90-100"


#: The thirteen Section 13.2 dimensions, in spec order.
DIMENSIONS: tuple[tuple[str, Callable[[TradeRecord], str]], ...] = (
    ("strategy", lambda r: r.strategy),
    ("regime", lambda r: r.regime),
    ("dte_band", lambda r: dte_band(r.dte_at_entry)),
    ("delta_band", lambda r: delta_band(r.net_delta_per_unit)),
    ("gamma_theta_band", gamma_theta_band),
    ("iv_state", lambda r: iv_state_band(r.iv_rank, r.term_structure_state)),
    ("spread_quality_band", lambda r: spread_quality_band(r.spread_pct_at_entry)),
    ("catalyst_type", lambda r: r.catalyst_type or "none"),
    ("underlying_sector", lambda r: f"{r.underlying}/{r.sector}"),
    ("entry_hour", entry_hour_band),
    ("holding_duration_band", lambda r: holding_duration_band(r.holding_days)),
    ("opportunity_score_band", lambda r: score_band(r.opportunity_score)),
    ("model_prompt", lambda r: f"{r.model_id}@{r.prompt_version}"),
)

DIMENSION_NAMES: tuple[str, ...] = tuple(name for name, _ in DIMENSIONS)


def bucket_key(record: TradeRecord) -> dict[str, str]:
    """The record's bucket along every dimension."""
    return {name: fn(record) for name, fn in DIMENSIONS}


def group_by_dimension(
    records: Sequence[TradeRecord], dimension: str
) -> dict[str, list[TradeRecord]]:
    """Group records into that dimension's buckets."""
    fn = dict(DIMENSIONS).get(dimension)
    if fn is None:
        raise KeyError(f"unknown calibration dimension {dimension!r}")
    grouped: dict[str, list[TradeRecord]] = {}
    for record in records:
        grouped.setdefault(fn(record), []).append(record)
    return grouped
