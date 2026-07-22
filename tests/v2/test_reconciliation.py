"""Reconciliation engine against the paper broker: converge, flag, block entries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg

from src.domain.instruments import LegSide, OptionContract, OptionType
from src.domain.orders import OrderState
from src.execution.interface import LimitOrderRequest, NetIntent, OrderLeg
from src.execution.order_state_machine import OrderStateMachine
from src.execution.paper_broker import PaperBroker
from src.execution.reconciliation import new_entries_allowed, reconcile

D = Decimal
NOW = datetime(2026, 7, 22, 15, 0, tzinfo=UTC)
EXP = datetime(2026, 8, 7, tzinfo=UTC).date()


def _request(key: str) -> LimitOrderRequest:
    contract = OptionContract("SPY", EXP, D("600"), OptionType.CALL)
    return LimitOrderRequest(
        idempotency_key=key,
        underlying="SPY",
        legs=(OrderLeg(contract, LegSide.BUY, 1),),
        limit_price=D("2.50"),
        net_intent=NetIntent.DEBIT,
        quantity=1,
    )


def _submitted_order(m: OrderStateMachine, broker: PaperBroker, key: str) -> tuple[Any, str]:
    order_id = m.create_order(idempotency_key=key)
    m.transition(order_id, OrderState.VALIDATED)
    m.transition(order_id, OrderState.STAGED)
    m.transition(order_id, OrderState.SUBMITTED)
    ack = broker.submit_order(_request(key))
    m.set_broker_order_id(order_id, ack.broker_order_id)
    return order_id, ack.broker_order_id


def test_broker_progress_is_applied_locally(conn: psycopg.Connection[Any]) -> None:
    m = OrderStateMachine(conn)
    broker = PaperBroker(starting_cash=D("1000"), clock=lambda: NOW)
    order_id, broker_id = _submitted_order(m, broker, "r-1")

    # Real wall-clock: submitted_at is stamped with real time by the machine, so
    # the stale-submitted check must be evaluated against real time here.
    real_now = datetime.now(UTC)
    report = reconcile(m, broker, now=real_now, conn=conn)
    assert report.checked == 1
    assert [a.action for a in report.advanced] == ["advanced"]  # SUBMITTED -> OPEN
    assert m.current_state(order_id) is OrderState.OPEN

    broker.fill(broker_id, 1, D("2.40"))
    report = reconcile(m, broker, now=real_now, conn=conn)
    assert m.current_state(order_id) is OrderState.FILLED
    assert report.clean
    allowed, reasons = new_entries_allowed(m, now=real_now)
    assert allowed and reasons == ()


def test_impossible_broker_state_flags_and_blocks_entries(
    conn: psycopg.Connection[Any],
) -> None:
    m = OrderStateMachine(conn)
    broker = PaperBroker(starting_cash=D("1000"), clock=lambda: NOW)
    order_id = m.create_order(idempotency_key="r-2")
    m.transition(order_id, OrderState.VALIDATED)  # never submitted locally...
    ack = broker.submit_order(_request("r-2"))
    broker.fill(ack.broker_order_id, 1, D("2.40"))  # ...but broker says FILLED
    m.set_broker_order_id(order_id, ack.broker_order_id)

    report = reconcile(m, broker, now=NOW, conn=conn)
    assert [f.action for f in report.flagged] == ["flagged"]
    assert m.current_state(order_id) is OrderState.RECONCILIATION_REQUIRED
    assert not report.clean

    allowed, reasons = new_entries_allowed(m, now=NOW)
    assert not allowed
    assert any("RECONCILIATION_REQUIRED" in r for r in reasons)

    event = conn.execute(
        "SELECT * FROM system_events WHERE event_type = 'order_state_mismatch'"
    ).fetchone()
    assert event is not None and event["severity"] == "critical"


def test_order_missing_at_broker_flags(conn: psycopg.Connection[Any]) -> None:
    m = OrderStateMachine(conn)
    broker = PaperBroker(starting_cash=D("1000"), clock=lambda: NOW)
    order_id = m.create_order(idempotency_key="r-3")
    m.transition(order_id, OrderState.VALIDATED)
    m.transition(order_id, OrderState.STAGED)
    m.transition(order_id, OrderState.SUBMITTED)
    m.set_broker_order_id(order_id, "P-GHOST")  # broker never saw this

    report = reconcile(m, broker, now=NOW, conn=conn)
    assert report.missing_at_broker == (order_id,)
    assert m.current_state(order_id) is OrderState.RECONCILIATION_REQUIRED
    allowed, _ = new_entries_allowed(m, now=NOW)
    assert not allowed


def test_unknown_broker_order_reported(conn: psycopg.Connection[Any]) -> None:
    m = OrderStateMachine(conn)
    broker = PaperBroker(starting_cash=D("1000"), clock=lambda: NOW)
    ack = broker.submit_order(_request("r-4"))  # submitted outside the machine
    report = reconcile(m, broker, now=NOW, conn=conn)
    assert report.unknown_at_local == (ack.broker_order_id,)
    assert not report.clean
    event = conn.execute(
        "SELECT * FROM system_events WHERE event_type = 'unknown_broker_order'"
    ).fetchone()
    assert event is not None


def test_stale_submitted_blocks_entries_after_timeout(
    conn: psycopg.Connection[Any],
) -> None:
    m = OrderStateMachine(conn)
    broker = PaperBroker(starting_cash=D("1000"), clock=lambda: NOW)
    order_id = m.create_order(idempotency_key="r-5")
    m.transition(order_id, OrderState.VALIDATED)
    m.transition(order_id, OrderState.STAGED)
    m.transition(order_id, OrderState.SUBMITTED)
    # No broker_order_id ever recorded: the ack never arrived.

    later = datetime.now(UTC) + timedelta(seconds=120)
    allowed, reasons = new_entries_allowed(m, now=later)
    assert not allowed
    assert any("unacknowledged" in r for r in reasons)

    report = reconcile(m, broker, now=later, conn=conn)
    assert report.stale_submitted == (order_id,)
    assert m.current_state(order_id) is OrderState.RECONCILIATION_REQUIRED


def test_within_timeout_submitted_does_not_block(
    conn: psycopg.Connection[Any],
) -> None:
    m = OrderStateMachine(conn)
    order_id = m.create_order(idempotency_key="r-6")
    m.transition(order_id, OrderState.VALIDATED)
    m.transition(order_id, OrderState.STAGED)
    m.transition(order_id, OrderState.SUBMITTED)
    allowed, _ = new_entries_allowed(m, now=datetime.now(UTC) + timedelta(seconds=5))
    assert allowed
