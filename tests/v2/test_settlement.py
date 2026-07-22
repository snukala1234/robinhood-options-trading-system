"""Settled-cash and collateral checks: exact boundaries, exits never blocked."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from src.domain.values import DomainValidationError
from src.risk.settlement import (
    CashAccountState,
    PendingProceeds,
    TradeRejected,
    assert_credit_trade_collateral_covered,
    assert_debit_trade_is_covered,
    buying_power,
    closing_order_cash_check,
    projected_settled_cash,
    required_collateral,
    settled_cash_available,
)

D = Decimal
TODAY = date(2026, 7, 21)
TOMORROW = date(2026, 7, 22)

STATE = CashAccountState(
    settled_cash=D("300.00"),
    pending=(PendingProceeds(amount=D("100.00"), settlement_date=TOMORROW),),
)


def test_unsettled_proceeds_excluded_until_settlement_date() -> None:
    assert settled_cash_available(STATE, TODAY) == D("300.00")
    assert settled_cash_available(STATE, TOMORROW) == D("400.00")
    assert projected_settled_cash(STATE, TOMORROW) == D("400.00")
    # Cash-account buying power IS settled cash.
    assert buying_power(STATE, TODAY) == D("300.00")


def test_debit_covered_exactly_at_the_boundary() -> None:
    assert_debit_trade_is_covered(D("300.00"), STATE, TODAY)  # exact: allowed


def test_one_cent_over_is_rejected() -> None:
    with pytest.raises(TradeRejected) as exc:
        assert_debit_trade_is_covered(D("300.01"), STATE, TODAY)
    assert exc.value.reason == "insufficient_settled_cash"


def test_debit_allowed_after_settlement_matures() -> None:
    assert_debit_trade_is_covered(D("400.00"), STATE, TOMORROW)
    with pytest.raises(TradeRejected):
        assert_debit_trade_is_covered(D("400.00"), STATE, TODAY)


def test_collateral_is_max_of_broker_and_own_calculation() -> None:
    assert required_collateral(D("380.00"), D("350.00")) == D("380.00")
    assert required_collateral(D("380.00"), D("420.00")) == D("420.00")
    # Broker requirement unknown -> trust only our own maximum-loss number.
    assert required_collateral(D("380.00"), None) == D("380.00")


def test_credit_collateral_must_be_settled_cash_covered() -> None:
    assert_credit_trade_collateral_covered(D("300.00"), STATE, TODAY)
    with pytest.raises(TradeRejected) as exc:
        assert_credit_trade_collateral_covered(D("380.00"), STATE, TODAY)
    assert exc.value.reason == "insufficient_settled_collateral"


def test_risk_reducing_exits_are_never_blocked() -> None:
    broke = CashAccountState(settled_cash=D("0"))
    # Closing a position with zero settled cash must be allowed: no exception.
    closing_order_cash_check(broke, TODAY)


def test_invalid_inputs_rejected() -> None:
    with pytest.raises(DomainValidationError):
        assert_debit_trade_is_covered(D("0"), STATE, TODAY)  # a zero debit is invalid
    with pytest.raises(DomainValidationError):
        assert_debit_trade_is_covered(300.0, STATE, TODAY)  # type: ignore[arg-type]
    with pytest.raises(DomainValidationError):
        PendingProceeds(amount=D("-5"), settlement_date=TOMORROW)
    with pytest.raises(DomainValidationError):
        CashAccountState(settled_cash=D("-1"))
    with pytest.raises(DomainValidationError):
        required_collateral(D("380.00"), D("0"))
