"""Committee aggregation (audit finding 2) and Section 9 sizing."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.agents.schemas import PortfolioManagerDecision, RiskOfficerDecision
from src.domain.values import DomainValidationError
from src.gate.committee import aggregate_committee
from src.risk.sizing import calculate_contract_quantity
from tests.v2.gate_harness import PM_APPROVE, RO_APPROVE

D = Decimal


def _pm_reduce(fraction: str) -> PortfolioManagerDecision:
    return PortfolioManagerDecision(
        action="reduce_risk", risk_fraction_of_request=fraction, rationale="concentration"
    )


def _ro_reduce(fraction: str) -> RiskOfficerDecision:
    return RiskOfficerDecision(
        decision="approve_with_reduction",
        reasons=["elevated event risk"],
        reduction_fraction=fraction,
    )


# --- committee ----------------------------------------------------------------


def test_veto_terminates_with_reasons() -> None:
    veto = RiskOfficerDecision(decision="veto", reasons=["earnings before expiration"])
    outcome = aggregate_committee(PM_APPROVE, veto)
    assert not outcome.proceed and outcome.veto
    assert outcome.effective_risk_fraction == 0
    assert outcome.reasons[0] == "risk_officer_veto"
    assert "earnings before expiration" in outcome.reasons


def test_double_approval_proceeds_at_full_fraction() -> None:
    outcome = aggregate_committee(PM_APPROVE, RO_APPROVE)
    assert outcome.proceed and not outcome.veto
    assert outcome.effective_risk_fraction == 1


@pytest.mark.parametrize(
    ("pm_fraction", "ro_fraction", "expected"),
    [("0.5", "0.8", "0.5"), ("0.9", "0.4", "0.4"), ("0.6", "0.6", "0.6")],
)
def test_smaller_reduction_always_wins(pm_fraction: str, ro_fraction: str, expected: str) -> None:
    outcome = aggregate_committee(_pm_reduce(pm_fraction), _ro_reduce(ro_fraction))
    assert outcome.proceed
    assert outcome.effective_risk_fraction == D(expected)


def test_single_sided_reduction_applies() -> None:
    assert aggregate_committee(_pm_reduce("0.5"), RO_APPROVE).effective_risk_fraction == D("0.5")
    assert aggregate_committee(PM_APPROVE, _ro_reduce("0.3")).effective_risk_fraction == D("0.3")


@pytest.mark.parametrize("action", ["hold_cash", "defer"])
def test_pm_no_entry_actions_do_not_proceed(action: str) -> None:
    pm = PortfolioManagerDecision(action=action, rationale="cash is a position")  # type: ignore[arg-type]
    outcome = aggregate_committee(pm, RO_APPROVE)
    assert not outcome.proceed and not outcome.veto
    assert f"portfolio_manager_{action}" in outcome.reasons


def test_agents_cannot_express_an_increase() -> None:
    """The schemas cap fractions at 1, so >1 is unrepresentable at the source."""
    with pytest.raises(ValueError):
        PortfolioManagerDecision(
            action="approve_for_gate", risk_fraction_of_request="1.5", rationale="x"
        )
    with pytest.raises(ValueError):
        RiskOfficerDecision(
            decision="approve_with_reduction", reasons=["x"], reduction_fraction="1.5"
        )


# --- sizing -------------------------------------------------------------------


def test_hand_verified_quantities() -> None:
    # equity 100k -> per-trade budget 1000; 1000 // 450 = 2
    assert calculate_contract_quantity(D("100000"), D("50000"), D("450"), D("0"), D("0")) == 2
    # settled cash is the binding budget: 800 // 450 = 1
    assert calculate_contract_quantity(D("100000"), D("800"), D("450"), D("0"), D("0")) == 1
    # portfolio remaining 5000 - 4600 = 400 < 450 -> no trade
    assert calculate_contract_quantity(D("100000"), D("50000"), D("450"), D("4600"), D("0")) == 0
    # cluster remaining 2000 - 1900 = 100 < 450 -> no trade
    assert calculate_contract_quantity(D("100000"), D("50000"), D("450"), D("0"), D("1900")) == 0
    # non-positive max loss per unit -> no trade, never a division guess
    assert calculate_contract_quantity(D("100000"), D("50000"), D("0"), D("0"), D("0")) == 0


def test_committee_fraction_scales_the_budget_down() -> None:
    # budget 1000 * 0.5 = 500 -> 500 // 450 = 1
    assert (
        calculate_contract_quantity(
            D("100000"), D("50000"), D("450"), D("0"), D("0"), risk_fraction=D("0.5")
        )
        == 1
    )
    assert (
        calculate_contract_quantity(
            D("100000"), D("50000"), D("450"), D("0"), D("0"), risk_fraction=D("0")
        )
        == 0
    )


def test_risk_fraction_above_one_is_rejected() -> None:
    """Agents may only reduce, never increase, the code-computed cap."""
    with pytest.raises(DomainValidationError, match="only reduce"):
        calculate_contract_quantity(
            D("100000"), D("50000"), D("450"), D("0"), D("0"), risk_fraction=D("1.01")
        )


def test_floats_are_rejected() -> None:
    with pytest.raises(DomainValidationError):
        calculate_contract_quantity(
            100000.0,  # type: ignore[arg-type]
            D("50000"),
            D("450"),
            D("0"),
            D("0"),
        )


_money = st.decimals(
    min_value="0.01", max_value="1000000", places=2, allow_nan=False, allow_infinity=False
)
_loss = st.decimals(
    min_value="0", max_value="100000", places=2, allow_nan=False, allow_infinity=False
)
_fraction = st.decimals(
    min_value="0", max_value="1", places=2, allow_nan=False, allow_infinity=False
)


@given(
    equity=_money,
    settled=_loss,
    per_unit=_money,
    open_risk=_loss,
    cluster=_loss,
    fraction=_fraction,
)
def test_property_sized_risk_never_exceeds_any_budget(
    equity: Decimal,
    settled: Decimal,
    per_unit: Decimal,
    open_risk: Decimal,
    cluster: Decimal,
    fraction: Decimal,
) -> None:
    quantity = calculate_contract_quantity(
        equity, settled, per_unit, open_risk, cluster, risk_fraction=fraction
    )
    assert quantity >= 0
    total = per_unit * quantity
    assert total <= equity * D("0.01") * fraction or quantity == 0
    assert total <= settled or quantity == 0
    assert open_risk + total <= equity * D("0.05") or quantity == 0
    assert cluster + total <= equity * D("0.02") or quantity == 0
