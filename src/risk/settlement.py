"""Settled-cash and buying-power checks for a cash account (spec Section 11).

Carries V1's settled-cash invariant into options: new debit positions draw only on
settled cash; credit-spread collateral is the *larger* of the broker requirement and
the independently calculated maximum loss (never trust a single source); unsettled
proceeds are unavailable for new entries; risk-reducing exits are never blocked by
settlement state.

All money is Decimal. In a cash account, buying power for new debit trades IS
settled cash — there is no margin.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from src.config.risk_policy import ENFORCE_SETTLED_CASH_ONLY
from src.domain.values import (
    DomainValidationError,
    require_non_negative_money,
    require_positive_money,
)


class TradeRejected(RuntimeError):
    """A cash/settlement guardrail rejected the trade. ``reason`` is machine-readable."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}{': ' + detail if detail else ''}")
        self.reason = reason


@dataclass(frozen=True)
class PendingProceeds:
    """Sale proceeds settling on ``settlement_date`` (T+1 for listed options)."""

    amount: Decimal
    settlement_date: date

    def __post_init__(self) -> None:
        require_positive_money("amount", self.amount)
        if not isinstance(self.settlement_date, date):
            raise DomainValidationError("settlement_date must be a date")


@dataclass(frozen=True)
class CashAccountState:
    """Point-in-time cash view: settled balance plus in-flight sale proceeds."""

    settled_cash: Decimal
    pending: tuple[PendingProceeds, ...] = ()

    def __post_init__(self) -> None:
        require_non_negative_money("settled_cash", self.settled_cash)


def settled_cash_available(state: CashAccountState, as_of: date) -> Decimal:
    """Settled balance plus any pending proceeds whose settlement date has passed."""
    if not isinstance(as_of, date):
        raise DomainValidationError("as_of must be a date")
    matured = sum((p.amount for p in state.pending if p.settlement_date <= as_of), Decimal("0"))
    return state.settled_cash + matured


def projected_settled_cash(state: CashAccountState, on: date) -> Decimal:
    """What will be settled on a future date if nothing else changes."""
    return settled_cash_available(state, on)


def buying_power(state: CashAccountState, as_of: date) -> Decimal:
    """Cash-account buying power for new debit trades: settled cash, nothing more."""
    return settled_cash_available(state, as_of)


def assert_debit_trade_is_covered(
    total_debit_including_fees: Decimal, state: CashAccountState, as_of: date
) -> None:
    """Reject a new debit trade not fully covered by settled cash (spec Section 11).

    Exact comparison — one cent over is a rejection, because a good-faith violation
    costs far more than a skipped entry.
    """
    require_positive_money("total_debit_including_fees", total_debit_including_fees)
    if not ENFORCE_SETTLED_CASH_ONLY:
        return
    available = settled_cash_available(state, as_of)
    if total_debit_including_fees > available:
        raise TradeRejected(
            "insufficient_settled_cash",
            f"debit {total_debit_including_fees} > settled {available}",
        )


def required_collateral(
    calculated_max_loss: Decimal, broker_requirement: Decimal | None
) -> Decimal:
    """Collateral to reserve for a credit structure: max(broker, own max loss).

    The broker number alone is never trusted (nor is any LLM's); when the broker
    requirement is unknown, the independently calculated maximum loss is used.
    """
    require_positive_money("calculated_max_loss", calculated_max_loss)
    if broker_requirement is None:
        return calculated_max_loss
    require_positive_money("broker_requirement", broker_requirement)
    return max(calculated_max_loss, broker_requirement)


def assert_credit_trade_collateral_covered(
    collateral: Decimal, state: CashAccountState, as_of: date
) -> None:
    """Reject a credit structure whose collateral is not fully settled-cash covered."""
    require_positive_money("collateral", collateral)
    if not ENFORCE_SETTLED_CASH_ONLY:
        return
    available = settled_cash_available(state, as_of)
    if collateral > available:
        raise TradeRejected(
            "insufficient_settled_collateral",
            f"collateral {collateral} > settled {available}",
        )


def closing_order_cash_check(_state: CashAccountState, _as_of: date) -> None:
    """Risk-reducing exits are never blocked by settlement state (spec Section 11).

    Present as an explicit function so the execution path calls *something* for
    every order kind and the asymmetry is visible and testable, not implicit.
    """
    return None
