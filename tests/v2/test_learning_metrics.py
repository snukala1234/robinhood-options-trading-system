"""Section 13.2 buckets and 13.3 metrics with hand-computed references."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from src.domain.values import DomainValidationError
from src.learning.buckets import (
    DIMENSION_NAMES,
    bucket_key,
    delta_band,
    dte_band,
    group_by_dimension,
    holding_duration_band,
    score_band,
    spread_quality_band,
)
from src.learning.metrics import compute_metrics
from src.learning.records import FillAttempt, TradeRecord

D = Decimal
ENTER = datetime(2026, 7, 10, 14, 30, tzinfo=UTC)  # 10:30 ET


def make_record(**overrides: Any) -> TradeRecord:
    kwargs: dict[str, Any] = {
        "trade_id": uuid.uuid4(),
        "strategy": "long_call",
        "regime": "trending_bullish",
        "dte_at_entry": 16,
        "net_delta_per_unit": D("0.40"),
        "delta_dollars": D("500"),
        "gamma_dollars": D("100"),
        "theta_dollars_daily": D("-10"),
        "iv_rank": D("40"),
        "term_structure_state": "contango",
        "spread_pct_at_entry": D("0.04"),
        "catalyst_type": "earnings",
        "underlying": "SPY",
        "sector": "index",
        "entered_at": ENTER,
        "exited_at": ENTER + timedelta(days=3),
        "opportunity_score": D("82"),
        "model_id": "alias-reasoning",
        "prompt_version": "market_regime/v1",
        "max_risk": D("1000"),
        "pnl_after_costs": D("100"),
        "costs": D("10"),
        "mae": D("50"),
        "mfe": D("150"),
        "slippage_vs_mid": D("2"),
        "predicted_win_probability": D("0.8"),
    }
    kwargs.update(overrides)
    return TradeRecord(**kwargs)


# --- buckets ------------------------------------------------------------------


def test_bucket_key_covers_all_thirteen_dimensions() -> None:
    key = bucket_key(make_record())
    assert tuple(key) == DIMENSION_NAMES
    assert len(key) == 13
    assert key == {
        "strategy": "long_call",
        "regime": "trending_bullish",
        "dte_band": "15-21",
        "delta_band": "25_45",
        "gamma_theta_band": "gamma_mid/theta_low",  # 100/1000=0.1, 10/1000=0.01
        "iv_state": "mid_contango",
        "spread_quality_band": "normal",
        "catalyst_type": "earnings",
        "underlying_sector": "SPY/index",
        "entry_hour": "10",
        "holding_duration_band": "2-3d",
        "opportunity_score_band": "80-90",
        "model_prompt": "alias-reasoning@market_regime/v1",
    }


def test_band_edges_are_exact() -> None:
    assert dte_band(7) == "0-7" and dte_band(8) == "8-14" and dte_band(22) == "22+"
    assert delta_band(D("-0.50")) == "45_60"  # magnitude banding
    assert delta_band(D("0.249999")) == "sub_25"
    assert spread_quality_band(D("0.02")) == "tight"
    assert spread_quality_band(D("0.1201")) == "very_wide"
    assert holding_duration_band(1) == "0-1d" and holding_duration_band(8) == "8d+"
    assert score_band(D("74.99")) == "sub_75" and score_band(D("90")) == "90-100"


def test_group_by_dimension() -> None:
    records = [make_record(), make_record(strategy="long_put", net_delta_per_unit=D("-0.40"))]
    grouped = group_by_dimension(records, "strategy")
    assert {k: len(v) for k, v in grouped.items()} == {"long_call": 1, "long_put": 1}
    with pytest.raises(KeyError):
        group_by_dimension(records, "astrology")


# --- metrics, hand-verified ---------------------------------------------------


def _trio() -> list[TradeRecord]:
    """Win p=0.8, loss p=0.7, win p=0.6 — the Brier reference trio."""
    return [
        make_record(
            pnl_after_costs=D("100"),
            predicted_win_probability=D("0.8"),
            exited_at=ENTER + timedelta(days=1),
        ),
        make_record(
            pnl_after_costs=D("-50"),
            predicted_win_probability=D("0.7"),
            exited_at=ENTER + timedelta(days=2),
        ),
        make_record(
            pnl_after_costs=D("40"),
            predicted_win_probability=D("0.6"),
            exited_at=ENTER + timedelta(days=3),
        ),
    ]


def test_metrics_match_hand_computation() -> None:
    metrics = compute_metrics(_trio())
    assert metrics.sample_size == 3
    assert metrics.win_rate == D("0.666667")
    assert metrics.avg_win == D("70.00")
    assert metrics.avg_loss == D("50.00")
    assert metrics.expectancy_after_costs == D("30.00")
    assert metrics.profit_factor == D("2.800000")  # 140 / 50
    assert metrics.avg_mae == D("50.00")
    assert metrics.avg_mfe == D("150.00")
    assert metrics.avg_slippage_vs_mid == D("2.00")
    # (0.1 - 0.05 + 0.04) / 3 on a 1000 max risk
    assert metrics.return_on_max_risk == D("0.030000")
    # (10 - 5 + 4) / 3 per |theta|=10
    assert metrics.return_per_unit_theta == D("3.000000")
    assert metrics.return_per_delta_dollar == D("0.060000")  # (0.2-0.1+0.08)/3
    assert metrics.return_per_gamma_dollar == D("0.300000")  # (1-0.5+0.4)/3


def test_brier_score_matches_reference() -> None:
    """((0.8-1)^2 + (0.7-0)^2 + (0.6-1)^2) / 3 = 0.69 / 3 = 0.23 exactly."""
    assert compute_metrics(_trio()).brier_score == D("0.230000")


def test_drawdown_and_recovery_hand_case() -> None:
    pnls = [D("100"), D("-50"), D("-60"), D("150")]
    records = [
        make_record(pnl_after_costs=pnl, exited_at=ENTER + timedelta(days=i + 1))
        for i, pnl in enumerate(pnls)
    ]
    metrics = compute_metrics(records)
    # Curve: 100, 50, -10, 140. Peak 100 -> trough -10 -> drawdown 110.
    assert metrics.max_drawdown == D("110.00")
    # Dip began at exit 2 (day 2), recovered at exit 4 (day 4): 2 days.
    assert metrics.longest_recovery_days == 2
    assert not metrics.in_drawdown_at_window_end


def test_open_drawdown_is_reported() -> None:
    metrics = compute_metrics(_trio())  # curve 100, 50, 90: still under peak
    assert metrics.max_drawdown == D("50.00")
    assert metrics.in_drawdown_at_window_end
    assert metrics.longest_recovery_days is None


def test_fill_metrics() -> None:
    attempts = [
        FillAttempt(True, D("10")),
        FillAttempt(True, D("20")),
        FillAttempt(True, D("30")),
        FillAttempt(False),
    ]
    metrics = compute_metrics(_trio(), attempts)
    assert metrics.fill_rate == D("0.750000")
    assert metrics.avg_seconds_to_fill == D("20.00")
    assert compute_metrics(_trio()).fill_rate is None  # no attempts, no fabrication


def test_no_losses_means_no_profit_factor_not_infinity() -> None:
    metrics = compute_metrics([make_record()])
    assert metrics.profit_factor is None
    assert metrics.avg_loss is None


def test_metrics_refuse_an_empty_sample() -> None:
    with pytest.raises(DomainValidationError, match="never fabricated"):
        compute_metrics([])


def test_json_round_trip_is_string_safe() -> None:
    doc = compute_metrics(_trio()).to_json()
    assert doc["win_rate"] == "0.666667"
    assert doc["profit_factor"] == "2.800000"
    assert doc["longest_recovery_days"] is None
    assert doc["in_drawdown_at_window_end"] is True


# --- record validation ---------------------------------------------------------


def test_records_reject_invalid_inputs() -> None:
    with pytest.raises(DomainValidationError):
        make_record(strategy="iron_condor")  # not in the registry
    with pytest.raises(DomainValidationError):
        make_record(predicted_win_probability=D("1.5"))
    with pytest.raises(DomainValidationError):
        make_record(iv_rank=D("150"))
    with pytest.raises(DomainValidationError):
        make_record(exited_at=ENTER - timedelta(days=1))
    with pytest.raises(DomainValidationError):
        make_record(pnl_after_costs=100.0)  # float money
    with pytest.raises(DomainValidationError):
        make_record(max_risk=D("0"))
    with pytest.raises(DomainValidationError):
        FillAttempt(True, None)  # filled requires a time
    with pytest.raises(DomainValidationError):
        FillAttempt(False, D("10"))  # unfilled cannot have one
