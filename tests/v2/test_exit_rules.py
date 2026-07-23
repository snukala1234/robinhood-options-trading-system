"""All five Section 10 exit dimensions: triggers, non-triggers, strict plans."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from src.domain.positions import ExitPlan
from src.domain.values import DomainValidationError
from src.execution.interface import NetIntent
from src.positions.exit_rules import evaluate_exits, exit_limit_price
from tests.v2.gate_harness import NOW
from tests.v2.position_harness import make_plan, make_position, make_state

D = Decimal


def _rules(evaluation: object) -> set[str]:
    return {s.rule for s in evaluation.signals}  # type: ignore[attr-defined]


def test_healthy_position_holds_with_no_signals() -> None:
    evaluation = evaluate_exits(make_state())
    assert evaluation.action == "hold" and evaluation.urgency == "low"
    assert evaluation.signals == ()
    assert evaluation.unrealized_pnl == 0


# --- 10.1 premium -------------------------------------------------------------


def test_hard_max_loss_threshold_exits_high_urgency() -> None:
    state = make_state(current_net_price=D("2.00"), bid=D("1.90"), ask=D("2.10"))
    evaluation = evaluate_exits(state)
    # (4.50 - 2.00) x 100 x 2 = 500 loss >= 450 plan threshold
    assert evaluation.action == "exit" and evaluation.urgency == "high"
    assert "hard_max_loss_threshold" in _rules(evaluation)
    assert evaluation.unrealized_pnl == D("-500")


def test_profit_target_exits() -> None:
    state = make_state(current_net_price=D("7.00"), bid=D("6.90"), ask=D("7.10"))
    evaluation = evaluate_exits(state)
    assert evaluation.action == "exit"
    assert "profit_target" in _rules(evaluation)


def test_scale_out_reduces() -> None:
    plan = make_plan(scale_out_net_price=D("6.00"))
    state = make_state(
        position=make_position(exit_plan=plan),
        current_net_price=D("6.20"),
        bid=D("6.10"),
        ask=D("6.30"),
    )
    evaluation = evaluate_exits(state)
    assert evaluation.action == "reduce"
    assert "scale_out" in _rules(evaluation)


# --- 10.2 underlying ----------------------------------------------------------


def test_thesis_invalidation_exits_high_urgency() -> None:
    evaluation = evaluate_exits(make_state(spot=D("589")))
    assert evaluation.action == "exit" and evaluation.urgency == "high"
    assert "thesis_invalidated" in _rules(evaluation)


def test_breakout_failure_exits() -> None:
    plan = make_plan(requires_breakout_above=D("600"))
    state = make_state(position=make_position(exit_plan=plan), spot=D("595"))
    evaluation = evaluate_exits(state)
    assert evaluation.action == "exit"
    assert "breakout_failure" in _rules(evaluation)


def test_trend_reversal_reviews() -> None:
    evaluation = evaluate_exits(make_state(trend_state="down"))
    assert evaluation.action == "review"
    assert "trend_reversal" in _rules(evaluation)


def test_support_violation_reduces() -> None:
    plan = make_plan(support_level=D("600"))
    state = make_state(
        position=make_position(exit_plan=plan), spot=D("598"), trend_state="sideways"
    )
    evaluation = evaluate_exits(state)
    assert evaluation.action == "reduce"
    assert "support_violation" in _rules(evaluation)


# --- 10.3 time ----------------------------------------------------------------


def test_max_holding_duration_exits() -> None:
    state = make_state(position=make_position(opened_at=NOW - timedelta(days=20)))
    evaluation = evaluate_exits(state)
    assert evaluation.action == "exit"
    assert "max_holding_duration" in _rules(evaluation)


def test_dte_review_checkpoint_reviews() -> None:
    evaluation = evaluate_exits(make_state(dte=5))
    assert evaluation.action == "review"
    assert "mandatory_dte_review" in _rules(evaluation)


def test_exit_before_expiration_by_default() -> None:
    evaluation = evaluate_exits(make_state(dte=2))
    assert evaluation.action == "exit" and evaluation.urgency == "high"
    assert "exit_before_expiration" in _rules(evaluation)


def test_accelerating_theta_alerts() -> None:
    evaluation = evaluate_exits(make_state(current_theta_daily_per_unit=D("-0.12")))
    assert evaluation.action == "alert"
    assert "accelerating_theta" in _rules(evaluation)


# --- 10.4 volatility ----------------------------------------------------------


def test_iv_crush_exits() -> None:
    evaluation = evaluate_exits(make_state(current_iv=D("0.20")))
    assert evaluation.action == "exit"
    assert "iv_crush" in _rules(evaluation)  # 0.20 <= 0.30 * 0.75


def test_favorable_vol_expansion_reviews() -> None:
    evaluation = evaluate_exits(make_state(current_iv=D("0.40")))
    assert evaluation.action == "review"
    assert "favorable_vol_expansion" in _rules(evaluation)  # 0.40 >= 0.30 * 1.30


def test_vol_regime_change_reviews() -> None:
    evaluation = evaluate_exits(make_state(vol_regime_changed=True))
    assert "vol_regime_change" in _rules(evaluation)
    assert evaluation.action == "review"


def test_vega_exhaustion_reviews() -> None:
    plan = make_plan(vega_floor_per_unit=D("0.05"))
    state = make_state(position=make_position(exit_plan=plan), current_vega_per_unit=D("0.02"))
    evaluation = evaluate_exits(state)
    assert "vega_exhausted" in _rules(evaluation)


def test_missing_iv_data_raises_an_alert_not_silence() -> None:
    evaluation = evaluate_exits(make_state(current_iv=None))
    assert evaluation.action == "alert"
    assert "iv_unavailable" in _rules(evaluation)


# --- 10.5 event ---------------------------------------------------------------


def test_catalyst_completed_reviews() -> None:
    evaluation = evaluate_exits(make_state(catalyst_completed=True))
    assert evaluation.action == "review"
    assert "catalyst_completed" in _rules(evaluation)


def test_new_material_event_exits_high_urgency() -> None:
    evaluation = evaluate_exits(make_state(new_material_event=True))
    assert evaluation.action == "exit" and evaluation.urgency == "high"
    assert "new_material_event" in _rules(evaluation)


def test_scheduled_event_entering_prohibited_window_exits() -> None:
    event = NOW.date() + timedelta(days=2)  # window is 3 days
    evaluation = evaluate_exits(make_state(next_scheduled_event_date=event))
    assert evaluation.action == "exit" and evaluation.urgency == "high"
    assert "event_prohibited_window" in _rules(evaluation)


def test_distant_scheduled_event_does_not_trigger() -> None:
    event = NOW.date() + timedelta(days=10)  # outside the 3-day window
    evaluation = evaluate_exits(make_state(next_scheduled_event_date=event))
    assert evaluation.action == "hold"


def test_trading_halt_alerts_high_urgency() -> None:
    evaluation = evaluate_exits(make_state(trading_halted=True))
    assert evaluation.action == "alert" and evaluation.urgency == "high"
    assert "trading_halt" in _rules(evaluation)


def test_abnormal_liquidity_alerts() -> None:
    evaluation = evaluate_exits(make_state(bid=D("0"), current_net_price=D("2.30")))
    assert "abnormal_liquidity" in _rules(evaluation)


# --- strict plans and aggregation ----------------------------------------------


def test_malformed_plan_raises_instead_of_evaluating_to_no_exit() -> None:
    broken = ExitPlan(
        premium={"note": "missing threshold"},
        underlying={"direction": "bullish", "invalidation_level": "590"},
        time={"max_holding_days": 15},
        volatility={"long_vega": True},
        event={"event_exit_days_before": 3},
    )
    with pytest.raises(DomainValidationError, match="max_loss_exit_usd"):
        evaluate_exits(make_state(position=make_position(exit_plan=broken)))


def test_float_in_plan_is_rejected() -> None:
    broken = ExitPlan(
        premium={"max_loss_exit_usd": 450.0},  # float: already drifted
        underlying={"direction": "bullish", "invalidation_level": "590"},
        time={"max_holding_days": 15},
        volatility={"long_vega": True},
        event={"event_exit_days_before": 3},
    )
    with pytest.raises(DomainValidationError, match="decimal string"):
        evaluate_exits(make_state(position=make_position(exit_plan=broken)))


def test_exit_always_outranks_softer_actions() -> None:
    # Hard loss (exit high) plus trend reversal (review) plus theta alert.
    state = make_state(
        current_net_price=D("2.00"),
        bid=D("1.90"),
        ask=D("2.10"),
        trend_state="down",
        current_theta_daily_per_unit=D("-0.12"),
    )
    evaluation = evaluate_exits(state)
    assert evaluation.action == "exit" and evaluation.urgency == "high"
    assert len(evaluation.signals) >= 3


# --- slippage-aware exit pricing ------------------------------------------------


def test_exit_limit_price_hand_verified() -> None:
    bid, ask = D("4.40"), D("4.60")  # mid 4.50, spread 0.20
    sell = exit_limit_price(bid, ask, opened_intent=NetIntent.DEBIT, urgency="medium")
    assert sell == D("4.45")  # concede a quarter spread downward
    sell_urgent = exit_limit_price(bid, ask, opened_intent=NetIntent.DEBIT, urgency="high")
    assert sell_urgent == D("4.40")  # concede half the spread
    buy_back = exit_limit_price(bid, ask, opened_intent=NetIntent.CREDIT, urgency="medium")
    assert buy_back == D("4.55")  # buying to close concedes upward
    buy_back_urgent = exit_limit_price(bid, ask, opened_intent=NetIntent.CREDIT, urgency="high")
    assert buy_back_urgent == D("4.60")


def test_exit_limit_price_never_below_one_cent() -> None:
    price = exit_limit_price(D("0"), D("0.02"), opened_intent=NetIntent.DEBIT, urgency="high")
    assert price == D("0.01")


def test_exit_limit_price_rejects_crossed_market() -> None:
    with pytest.raises(DomainValidationError):
        exit_limit_price(D("5"), D("4"), opened_intent=NetIntent.DEBIT)


def test_state_rejects_invalid_inputs() -> None:
    with pytest.raises(DomainValidationError):
        make_state(dte=-1)
    with pytest.raises(DomainValidationError):
        make_state(spot=D("0"))
    with pytest.raises(DomainValidationError):
        make_state(bid=D("5.00"))  # crossed vs ask 4.60
    with pytest.raises(DomainValidationError):
        make_state(snapshot_ids=())
    with pytest.raises(DomainValidationError):
        make_state(spot=605.0)  # float money
    with pytest.raises(DomainValidationError):
        make_state(as_of=NOW.replace(tzinfo=None))
    with pytest.raises(DomainValidationError):
        make_state(trend_state="upward")
