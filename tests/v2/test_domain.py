"""Domain model validation: Decimal-only money, frozen shapes, legal order states."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from src.domain.instruments import Leg, LegSide, OptionContract, OptionType
from src.domain.orders import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    OrderIntent,
    OrderState,
    can_transition,
)
from src.domain.portfolio import PortfolioSnapshot
from src.domain.positions import EXIT_DIMENSIONS, ExitPlan, Position, PositionStatus
from src.domain.proposals import Direction, TradeProposal
from src.domain.values import DomainValidationError, require_money

UTC_NOW = datetime(2026, 7, 21, 14, 0, tzinfo=UTC)


def _exit_plan() -> ExitPlan:
    return ExitPlan(
        premium={"max_loss_pct": "0.5"},
        underlying={"invalidation_level": "590"},
        time={"dte_forced_exit": 2},
        volatility={"iv_crush_exit": True},
        event={"exit_before_earnings": True},
    )


def _legs() -> tuple[Leg, Leg]:
    return (
        Leg(LegSide.BUY, OptionType.CALL, Decimal("600"), 1),
        Leg(LegSide.SELL, OptionType.CALL, Decimal("605"), 1),
    )


def _proposal(**overrides: object) -> TradeProposal:
    kwargs: dict[str, object] = {
        "underlying": "SPY",
        "direction": Direction.BULLISH,
        "strategy": "bull_call_debit_spread",
        "expiration": date(2026, 8, 7),
        "dte": 17,
        "legs": _legs(),
        "limit_price": Decimal("1.85"),
        "max_loss": Decimal("185.00"),
        "max_gain": Decimal("315.00"),
        "breakevens": (Decimal("601.85"),),
        "net_delta": Decimal("0.24"),
        "net_gamma": Decimal("0.04"),
        "net_theta_daily": Decimal("-6.20"),
        "net_vega": Decimal("8.10"),
        "config_version_id": uuid.uuid4(),
    }
    kwargs.update(overrides)
    return TradeProposal(**kwargs)  # type: ignore[arg-type]


# --- money discipline --------------------------------------------------------


def test_money_rejects_float_int_and_str() -> None:
    for bad in (1.85, 1, "1.85", True):
        with pytest.raises(DomainValidationError):
            require_money("x", bad)
    assert require_money("x", Decimal("1.85")) == Decimal("1.85")


def test_proposal_rejects_float_money() -> None:
    with pytest.raises(DomainValidationError):
        _proposal(limit_price=1.85)
    with pytest.raises(DomainValidationError):
        _proposal(max_loss=185.0)
    with pytest.raises(DomainValidationError):
        _proposal(net_delta=0.24)


# --- instruments -------------------------------------------------------------


def test_occ_symbol() -> None:
    contract = OptionContract("spy", date(2026, 9, 18), Decimal("600"), OptionType.CALL)
    assert contract.occ_symbol() == "SPY260918C00600000"
    assert contract.underlying == "SPY"  # normalized upper-case


def test_leg_validation() -> None:
    with pytest.raises(DomainValidationError):
        Leg(LegSide.BUY, OptionType.CALL, Decimal("600"), 0)
    with pytest.raises(DomainValidationError):
        Leg(LegSide.BUY, OptionType.CALL, Decimal("-1"), 1)


# --- proposals ---------------------------------------------------------------


def test_valid_proposal_constructs() -> None:
    p = _proposal()
    assert p.strategy == "bull_call_debit_spread"
    assert p.max_loss == Decimal("185.00")


def test_proposal_is_frozen() -> None:
    p = _proposal()
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.max_loss = Decimal("1")  # type: ignore[misc]


def test_unknown_strategy_rejected() -> None:
    with pytest.raises(KeyError):
        _proposal(strategy="iron_condor")


def test_leg_count_must_match_registry() -> None:
    with pytest.raises(DomainValidationError, match="requires 2 leg"):
        _proposal(legs=_legs()[:1])


def test_negative_dte_rejected() -> None:
    with pytest.raises(DomainValidationError):
        _proposal(dte=-1)


# --- order state machine -----------------------------------------------------


def test_happy_path_transitions() -> None:
    path = [
        OrderState.CREATED,
        OrderState.VALIDATED,
        OrderState.STAGED,
        OrderState.SUBMITTED,
        OrderState.OPEN,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
    ]
    for current, nxt in zip(path, path[1:], strict=False):
        assert can_transition(current, nxt), f"{current} -> {nxt}"


def test_illegal_transitions_rejected() -> None:
    assert not can_transition(OrderState.CREATED, OrderState.OPEN)
    assert not can_transition(OrderState.FILLED, OrderState.OPEN)
    assert not can_transition(OrderState.STAGED, OrderState.FILLED)


def test_reconciliation_reachable_from_every_state() -> None:
    for state in OrderState:
        if state is OrderState.RECONCILIATION_REQUIRED:
            continue
        assert can_transition(state, OrderState.RECONCILIATION_REQUIRED), state


def test_terminal_states_only_allow_reconciliation() -> None:
    for state in TERMINAL_STATES:
        assert ALLOWED_TRANSITIONS[state] == frozenset({OrderState.RECONCILIATION_REQUIRED}), state


def test_order_intent_idempotency_key_is_deterministic() -> None:
    pid = uuid.uuid4()
    a = OrderIntent(pid, Decimal("1.85"), 1)
    b = OrderIntent(pid, Decimal("1.85"), 1)
    assert a.idempotency_key == b.idempotency_key == f"{pid}:1"
    assert OrderIntent(pid, Decimal("1.85"), 1, attempt=2).idempotency_key == f"{pid}:2"


# --- positions and exit plans ------------------------------------------------


def test_exit_plan_requires_all_five_dimensions() -> None:
    assert EXIT_DIMENSIONS == ("premium", "underlying", "time", "volatility", "event")
    with pytest.raises(DomainValidationError, match="volatility"):
        ExitPlan(
            premium={"x": 1},
            underlying={"x": 1},
            time={"x": 1},
            volatility={},
            event={"x": 1},
        )


def test_position_requires_exit_plan_and_utc() -> None:
    pos = Position(
        proposal_id=uuid.uuid4(),
        underlying="SPY",
        strategy="bull_call_debit_spread",
        expiration=date(2026, 8, 7),
        legs=_legs(),
        opened_at=UTC_NOW,
        entry_net_price=Decimal("1.85"),
        quantity=1,
        max_loss=Decimal("185.00"),
        status=PositionStatus.OPEN,
        exit_plan=_exit_plan(),
    )
    assert pos.status is PositionStatus.OPEN
    with pytest.raises(DomainValidationError, match="timezone-aware"):
        dataclasses.replace(pos, opened_at=datetime(2026, 7, 21, 14, 0))
    with pytest.raises(DomainValidationError, match="closed_at"):
        dataclasses.replace(pos, status=PositionStatus.CLOSED)


def test_portfolio_snapshot_validation() -> None:
    snap = PortfolioSnapshot(
        observed_at=UTC_NOW,
        total_equity=Decimal("1000.00"),
        settled_cash=Decimal("400.00"),
        unsettled_cash=Decimal("100.00"),
        open_risk=Decimal("30.00"),
    )
    assert snap.is_paper is True
    with pytest.raises(DomainValidationError):
        dataclasses.replace(snap, total_equity=Decimal("-1"))
    with pytest.raises(DomainValidationError):
        dataclasses.replace(snap, settled_cash=400.0)  # type: ignore[arg-type]
