"""Attempted-bypass tests: every guardrail step, attacked directly, fails closed.

Each test builds an input that is green EXCEPT for one engineered attack and
proves the gate rejects at exactly the expected precedence step — or, for the
structural attacks (forged tokens, out-of-order evaluation), that the attack
is impossible by construction.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from src.analytics.portfolio_exposure import PositionExposure, aggregate
from src.data.option_chains import ContractQuote
from src.domain.instruments import Leg, LegSide, OptionContract, OptionType
from src.domain.proposals import Direction, TradeProposal
from src.execution.capabilities import BrokerCapabilities
from src.gate.kill_switches import KillSwitchPanel
from src.gate.trade_gate import (
    ApprovalToken,
    CircuitBreakerInputs,
    GateInput,
    GateResult,
    GateViolation,
    TradeGate,
)
from src.risk.settlement import CashAccountState
from tests.v2.gate_harness import (
    EXPIRATION,
    NOW,
    make_account,
    make_input,
    make_proposal,
    make_quote,
)

D = Decimal
SRC = Path(__file__).resolve().parents[2] / "src"

PUT_600 = OptionContract(
    underlying="SPY", expiration=EXPIRATION, strike=D("600"), option_type=OptionType.PUT
)
PUT_590 = OptionContract(
    underlying="SPY", expiration=EXPIRATION, strike=D("590"), option_type=OptionType.PUT
)


def _evaluate(gi: GateInput, panel: KillSwitchPanel | None = None) -> GateResult:
    return TradeGate(panel=panel or KillSwitchPanel(), clock=lambda: NOW).evaluate(gi)


def _rejected_at(result: GateResult, step: str) -> None:
    assert not result.approved and result.token is None
    assert result.rejection_step == step


def _credit_spread(**overrides: Any) -> TradeProposal:
    kwargs: dict[str, Any] = {
        "underlying": "SPY",
        "direction": Direction.BULLISH,
        "strategy": "put_credit_spread",
        "expiration": EXPIRATION,
        "dte": 16,
        "legs": (
            Leg(LegSide.SELL, OptionType.PUT, D("600"), 1),
            Leg(LegSide.BUY, OptionType.PUT, D("590"), 1),
        ),
        "limit_price": D("2.00"),
        "max_loss": D("450"),
        "max_gain": D("200"),
        "breakevens": (D("598.00"),),
        "net_delta": D("0.15"),
        "net_gamma": D("0.01"),
        "net_theta_daily": D("0.03"),
        "net_vega": D("-0.05"),
        "config_version_id": uuid.uuid4(),
    }
    kwargs.update(overrides)
    return TradeProposal(**kwargs)


def _spread_quotes(mid_short: str, mid_long: str) -> tuple[ContractQuote, ...]:
    short_mid, long_mid = D(mid_short), D(mid_long)
    return (
        make_quote(
            contract=PUT_600,
            bid=short_mid - D("0.05"),
            ask=short_mid + D("0.05"),
            midpoint=short_mid,
        ),
        make_quote(
            contract=PUT_590,
            bid=long_mid - D("0.05"),
            ask=long_mid + D("0.05"),
            midpoint=long_mid,
        ),
    )


# --- step 1: system health and data freshness ---------------------------------


def test_stale_quote_cannot_be_approved() -> None:
    stale = make_quote(observed_at=NOW - timedelta(seconds=6))
    result = _evaluate(make_input(leg_quotes=(stale,)))
    _rejected_at(result, "system_health_and_data_freshness")
    assert any("quote is" in r for r in result.reasons)


def test_future_dated_quote_is_clock_skew_not_freshness() -> None:
    skewed = make_quote(observed_at=NOW + timedelta(seconds=3))
    _rejected_at(_evaluate(make_input(leg_quotes=(skewed,))), "system_health_and_data_freshness")


def test_active_kill_switch_blocks_at_step_one() -> None:
    panel = KillSwitchPanel()
    panel.activate("market_data_degradation", reason="test")
    result = _evaluate(make_input(), panel=panel)
    _rejected_at(result, "system_health_and_data_freshness")
    assert any("market_data_degradation" in r for r in result.reasons)


def test_order_state_uncertainty_blocks_at_step_one() -> None:
    gi = make_input(reconciliation_blocked_reasons=("1 order(s) in RECONCILIATION_REQUIRED",))
    _rejected_at(_evaluate(gi), "system_health_and_data_freshness")


def test_failover_decisions_cannot_open_positions() -> None:
    _rejected_at(
        _evaluate(make_input(decided_under_failover=True)),
        "system_health_and_data_freshness",
    )


def test_stale_account_snapshot_blocks() -> None:
    old_account = make_account(observed_at=NOW - timedelta(seconds=120))
    _rejected_at(_evaluate(make_input(account=old_account)), "system_health_and_data_freshness")


def test_stale_underlying_data_blocks() -> None:
    _rejected_at(
        _evaluate(make_input(underlying_data_age_seconds=11.0)),
        "system_health_and_data_freshness",
    )


# --- step 2: broker/account capability ----------------------------------------


def test_capability_gated_strategy_cannot_reach_submission() -> None:
    single_leg_only = BrokerCapabilities(
        account_read=True,
        option_chain_read=True,
        single_leg_orders=True,
        limit_orders=True,
        price_increment=D("0.01"),
    )
    gi = make_input(
        proposal=_credit_spread(),
        leg_quotes=_spread_quotes("2.05", "1.05"),
        capabilities=single_leg_only,
    )
    result = _evaluate(gi)
    _rejected_at(result, "broker_account_capability")
    assert any("not executable" in r for r in result.reasons)


def test_price_increment_violation_blocks() -> None:
    gi = make_input(
        proposal=make_proposal(limit_price=D("4.505")),
        cash_state=CashAccountState(settled_cash=D("50000")),
    )
    _rejected_at(_evaluate(gi), "broker_account_capability")


# --- step 3: settlement and buying power --------------------------------------


def test_settled_cash_shortfall_blocks_a_debit() -> None:
    # Cash 460 sizes exactly one 450 contract, but fees push the debit to 470.
    gi = make_input(cash_state=CashAccountState(settled_cash=D("460")), estimated_fees=D("20"))
    result = _evaluate(gi)
    _rejected_at(result, "settlement_and_buying_power")
    assert any("insufficient_settled_cash" in r for r in result.reasons)


def test_per_trade_budget_overflow_sizes_to_zero_and_blocks() -> None:
    """A proposal whose unit max loss exceeds the whole 1% budget can never
    produce a quantity, so it can never get a token."""
    gi = make_input(proposal=make_proposal(max_loss=D("2000")))
    result = _evaluate(gi)
    _rejected_at(result, "settlement_and_buying_power")
    assert any("quantity is zero" in r for r in result.reasons)


def test_credit_collateral_uses_max_of_broker_and_own_and_blocks_shortfall() -> None:
    gi = make_input(
        proposal=_credit_spread(),
        leg_quotes=_spread_quotes("2.05", "1.05"),
        cash_state=CashAccountState(settled_cash=D("460")),
        broker_collateral_requirement=D("500"),  # larger than our 450 -> binding
    )
    result = _evaluate(gi)
    _rejected_at(result, "settlement_and_buying_power")
    assert any("insufficient_settled_collateral" in r for r in result.reasons)


# --- step 4: strategy permission ----------------------------------------------


@pytest.mark.parametrize("dte", [0, 2, 40])
def test_dte_outside_absolute_bounds_blocks(dte: int) -> None:
    _rejected_at(_evaluate(make_input(proposal=make_proposal(dte=dte))), "strategy_permission")


def test_earnings_before_expiration_blocks() -> None:
    _rejected_at(_evaluate(make_input(earnings_before_expiration=True)), "strategy_permission")


# --- step 5: per-trade maximum loss -------------------------------------------


def test_structure_price_above_equity_cap_blocks() -> None:
    # Small per-unit max loss sizes fine, but the structure costs 3500/unit
    # against a 3000 cap (3% of equity).
    gi = make_input(
        proposal=_credit_spread(limit_price=D("35.00"), max_loss=D("100"), max_gain=D("3500")),
        leg_quotes=_spread_quotes("35.00", "31.00"),
    )
    result = _evaluate(gi)
    _rejected_at(result, "per_trade_maximum_loss")
    assert any("MAX_CONTRACT_PRICE_PCT_OF_EQUITY" in r for r in result.reasons)


# --- step 6: portfolio exposure -----------------------------------------------


def _spy_position(max_loss: str) -> PositionExposure:
    return PositionExposure(
        position_id="p1",
        underlying="SPY",
        sector="index",
        strategy="long_call",
        expiration=EXPIRATION,
        spot=D("600"),
        net_delta_per_unit=D("0.001"),
        net_gamma_per_unit=D("0"),
        net_theta_daily_per_unit=D("0"),
        net_vega_per_unit=D("0"),
        quantity=1,
        multiplier=100,
        max_loss=D(max_loss),
    )


def test_single_underlying_concentration_blocks() -> None:
    portfolio = aggregate((_spy_position("1200"),), account_equity=D("100000"))
    gi = make_input(portfolio=portfolio, open_position_count=1)
    result = _evaluate(gi)
    # Existing 1200 + new 900 = 2100 > 1500 (1.5% of equity).
    _rejected_at(result, "portfolio_exposure")
    assert any("single-underlying cap" in r for r in result.reasons)


def test_max_concurrent_positions_blocks() -> None:
    _rejected_at(_evaluate(make_input(open_position_count=3)), "portfolio_exposure")


# --- step 7: circuit breakers --------------------------------------------------


@pytest.mark.parametrize(
    ("field", "loss"),
    [
        ("daily_realized_loss", "2500"),  # > 2% of 100k
        ("daily_equity_drawdown", "2600"),  # > 2.5%
        ("weekly_drawdown", "5100"),  # > 5%
        ("peak_to_trough_drawdown", "10100"),  # > 10%
    ],
)
def test_each_circuit_breaker_blocks_and_trips_the_panel(field: str, loss: str) -> None:
    values = {
        "daily_realized_loss": D("0"),
        "daily_equity_drawdown": D("0"),
        "weekly_drawdown": D("0"),
        "peak_to_trough_drawdown": D("0"),
    }
    values[field] = D(loss)
    panel = KillSwitchPanel()
    result = _evaluate(make_input(breakers=CircuitBreakerInputs(**values)), panel=panel)
    _rejected_at(result, "circuit_breakers")
    # The breach tripped the panel: epoch bumped, so ANY previously issued
    # token is now invalid at the adapter as well.
    assert panel.halt_epoch == 1
    assert panel.blocks_new_entries() == (f"{field}_breach",)


# --- step 8: liquidity and execution -------------------------------------------


def test_wide_spread_blocks() -> None:
    wide = make_quote(bid=D("3.00"), ask=D("6.00"), midpoint=D("4.50"))
    result = _evaluate(make_input(leg_quotes=(wide,)))
    _rejected_at(result, "liquidity_and_execution")
    assert any("spread_pct" in r for r in result.reasons)


def test_thin_open_interest_blocks() -> None:
    thin = make_quote(open_interest=10)
    _rejected_at(_evaluate(make_input(leg_quotes=(thin,))), "liquidity_and_execution")


# --- step 9: human approval policy ---------------------------------------------


def test_live_destination_is_always_refused() -> None:
    result = _evaluate(make_input(destination="live"))
    _rejected_at(result, "human_approval_policy")
    assert any("live orders are disabled" in r for r in result.reasons)


# --- precedence is structural ---------------------------------------------------


def test_downstream_pass_can_never_override_an_upstream_fail() -> None:
    """An input failing steps 1, 8, and 9 simultaneously rejects at step 1 and
    the later failing steps are never even evaluated."""
    stale_and_wide = make_quote(
        bid=D("3.00"),
        ask=D("6.00"),
        midpoint=D("4.50"),
        observed_at=NOW - timedelta(seconds=30),
    )
    result = _evaluate(make_input(leg_quotes=(stale_and_wide,), destination="live"))
    _rejected_at(result, "system_health_and_data_freshness")
    statuses = {s.name: s.status for s in result.steps}
    assert statuses["system_health_and_data_freshness"] == "rejected"
    for later in (
        "broker_account_capability",
        "settlement_and_buying_power",
        "strategy_permission",
        "per_trade_maximum_loss",
        "portfolio_exposure",
        "circuit_breakers",
        "liquidity_and_execution",
        "human_approval_policy",
        "order_submission",
    ):
        assert statuses[later] == "not_evaluated"


# --- forging tokens is impossible ------------------------------------------------


def test_a_token_cannot_be_forged_outside_the_gate() -> None:
    with pytest.raises(GateViolation, match="only be minted"):
        ApprovalToken(
            token_id=uuid.uuid4(),
            proposal_id=uuid.uuid4(),
            account_state_hash="x",
            quote_snapshot_hash="y",
            halt_epoch=0,
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=30),
            approved_quantity=1,
            limit_price=D("4.50"),
            total_max_loss=D("450"),
            correlation_id=uuid.uuid4(),
            _mint=object(),
        )


def test_mint_capability_is_private_to_the_gate_module() -> None:
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if path.relative_to(SRC).as_posix() != "gate/trade_gate.py"
        and "_MINT" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_no_module_but_the_gate_constructs_approval_tokens() -> None:
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if path.relative_to(SRC).as_posix() != "gate/trade_gate.py"
        and "ApprovalToken(" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
