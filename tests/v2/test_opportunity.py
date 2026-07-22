"""Opportunity score and opportunity-cost engine — exact expected values."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from src.analytics.opportunity_cost import (
    CandidateEconomics,
    evaluate,
    rank_candidates,
)
from src.analytics.opportunity_score import (
    ScoreComponents,
    compute_score,
)
from src.domain.values import DomainValidationError

D = Decimal


def _components(value: str, penalty: str = "0") -> ScoreComponents:
    v = D(value)
    return ScoreComponents(
        directional_edge=v,
        gamma_efficiency=v,
        theta_efficiency=v,
        volatility_fit=v,
        liquidity=v,
        catalyst_quality=v,
        technical_structure=v,
        market_regime_fit=v,
        portfolio_fit=v,
        execution_quality=v,
        expected_value_after_costs=v,
        risk_penalty=D(penalty),
    )


# --- score -------------------------------------------------------------------


def test_perfect_components_score_100() -> None:
    score = compute_score(_components("1"))
    assert score.total == D("100.00")
    # Default Section 5.8 weights sum to exactly 100.
    assert sum(score.weighted.values()) == D("100.00")


def test_uniform_half_components_score_50() -> None:
    assert compute_score(_components("0.5")).total == D("50.00")


def test_risk_penalty_is_subtractive() -> None:
    assert compute_score(_components("0.5", penalty="7.5")).total == D("42.50")


def test_shared_weight_components_average() -> None:
    """directional_edge=1 with technical_structure=0 earns half the 15-pt weight."""
    comps = replace(_components("0"), directional_edge=D("1"))
    score = compute_score(comps)
    assert score.weighted["directional_technical_edge"] == D("7.50")
    assert score.total == D("7.50")


def test_score_clamped_to_zero_floor() -> None:
    assert compute_score(_components("0", penalty="25")).total == D("0.00")


def test_component_out_of_range_rejected() -> None:
    with pytest.raises(DomainValidationError, match="liquidity"):
        replace(_components("0.5"), liquidity=D("1.2"))
    with pytest.raises(DomainValidationError, match="risk_penalty"):
        _components("0.5", penalty="-1")
    with pytest.raises(DomainValidationError):
        replace(_components("0.5"), volatility_fit=0.5)  # type: ignore[arg-type]


# --- opportunity cost --------------------------------------------------------

STRONG = compute_score(_components("1"))  # 100.00
WEAK = compute_score(_components("0.5"))  # 50.00 -> below MIN_OPPORTUNITY_SCORE


def _candidate(cid: str, **overrides: object) -> CandidateEconomics:
    kwargs: dict[str, object] = {
        "candidate_id": cid,
        "score": STRONG,
        "max_loss": D("250.00"),
        "expected_return_after_costs": D("400.00"),
        "capital_required": D("250.00"),
        "theta_dollars_daily": D("-5.00"),
        "correlation_with_portfolio": D("0.2"),
    }
    kwargs.update(overrides)
    return CandidateEconomics(**kwargs)  # type: ignore[arg-type]


BUDGET = {
    "account_equity": D("30000"),  # per-trade budget 300, total budget 1500
    "settled_cash": D("1000"),
    "current_open_risk": D("500"),
}


def test_derived_ratios_hand_verified() -> None:
    c = _candidate("a")
    assert c.return_per_risk == D("1.600000")
    assert c.return_per_capital == D("1.600000")
    # 400 / 5.00 of daily decay = 80 per theta-dollar
    assert c.return_per_theta_dollar == D("80.000000")
    assert _candidate("b", theta_dollars_daily=D("1.00")).return_per_theta_dollar is None


def test_eligible_candidate_passes_all_gates() -> None:
    r = evaluate(_candidate("a"), weakest_open_score=D("80"), **BUDGET)
    assert r.eligible and r.rejection_reasons == ()
    assert r.replacement_value == D("20.00")


def test_each_gate_produces_a_reason() -> None:
    cases = {
        "per-trade budget": _candidate("b", max_loss=D("350.00")),
        "settled cash": _candidate("c", capital_required=D("5000.00")),
        "below minimum": _candidate("d", score=WEAK),
        "not positive": _candidate("e", expected_return_after_costs=D("-10.00")),
    }
    for fragment, candidate in cases.items():
        r = evaluate(candidate, **BUDGET)
        assert not r.eligible
        assert any(fragment in reason for reason in r.rejection_reasons), fragment


def test_portfolio_risk_budget_gate() -> None:
    r = evaluate(
        _candidate("f", max_loss=D("299.00")),
        account_equity=D("30000"),
        settled_cash=D("1000"),
        current_open_risk=D("1400"),
    )  # only 100 remaining of 1500
    assert not r.eligible
    assert any("remaining portfolio risk" in reason for reason in r.rejection_reasons)


def test_ranking_orders_by_return_per_risk() -> None:
    a = _candidate("a")  # 1.6 per unit risk
    b = _candidate("b", expected_return_after_costs=D("200.00"))  # 0.8
    ineligible = _candidate("c", score=WEAK)
    ranked = rank_candidates([b, ineligible, a], **BUDGET)
    ordered = [(r.candidate.candidate_id, r.rank, r.eligible) for r in ranked]
    assert ordered[0] == ("a", 1, True)
    assert ordered[1] == ("b", 2, True)
    assert ordered[2] == ("c", 0, False)


def test_empty_eligible_set_is_valid_cash_is_a_position() -> None:
    ranked = rank_candidates([_candidate("x", score=WEAK)], **BUDGET)
    assert all(not r.eligible for r in ranked)


def test_candidate_economics_validation() -> None:
    with pytest.raises(DomainValidationError):
        _candidate("bad", max_loss=D("0"))
    with pytest.raises(DomainValidationError):
        _candidate("bad", correlation_with_portfolio=D("1.5"))
    with pytest.raises(DomainValidationError):
        _candidate("bad", expected_return_after_costs=400.0)
