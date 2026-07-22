"""Order state machine on real Postgres: idempotency, transitions, append-only log."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from psycopg import errors

from src.domain.orders import OrderState
from src.execution.interface import DuplicateOrderError
from src.execution.order_state_machine import OrderStateMachine


def _machine(conn: psycopg.Connection[Any]) -> OrderStateMachine:
    return OrderStateMachine(conn)


def test_create_and_full_happy_path(conn: psycopg.Connection[Any]) -> None:
    m = _machine(conn)
    order_id = m.create_order(idempotency_key="k-1", raw_request={"limit": "1.85"})
    assert m.current_state(order_id) is OrderState.CREATED
    path = [
        OrderState.VALIDATED,
        OrderState.STAGED,
        OrderState.SUBMITTED,
        OrderState.OPEN,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
    ]
    for state in path:
        result = m.transition(order_id, state)
        assert result.applied_as_requested, state
    assert m.current_state(order_id) is OrderState.FILLED
    events = m.events(order_id)
    assert len(events) == 7  # create + 6 transitions
    assert events[0]["previous_state"] is None
    assert events[-1]["new_state"] == "FILLED"
    row = m.get_order(order_id)
    assert row is not None
    assert row["current_state"] == "FILLED"
    assert row["submitted_at"] is not None  # stamped on SUBMITTED


def test_duplicate_idempotency_key_rejected_in_code(
    conn: psycopg.Connection[Any],
) -> None:
    m = _machine(conn)
    first = m.create_order(idempotency_key="k-dup")
    with pytest.raises(DuplicateOrderError) as exc:
        m.create_order(idempotency_key="k-dup")
    assert exc.value.existing_id == str(first)
    n = conn.execute("SELECT COUNT(*) AS n FROM orders WHERE idempotency_key = 'k-dup'").fetchone()
    assert n is not None and n["n"] == 1


def test_duplicate_key_rejected_by_database_constraint(
    conn: psycopg.Connection[Any],
) -> None:
    """Even raw SQL past the code check dies on the UNIQUE constraint."""
    m = _machine(conn)
    m.create_order(idempotency_key="k-raw")
    with pytest.raises(errors.UniqueViolation), conn.transaction():
        conn.execute(
            "INSERT INTO orders (id, idempotency_key, current_state) "
            "VALUES (%s, 'k-raw', 'CREATED')",
            (uuid.uuid4(),),
        )


def test_illegal_transition_flags_reconciliation_not_applied(
    conn: psycopg.Connection[Any],
) -> None:
    m = _machine(conn)
    order_id = m.create_order(idempotency_key="k-ill")
    result = m.transition(order_id, OrderState.FILLED)  # CREATED -> FILLED illegal
    assert not result.applied_as_requested
    assert result.new_state is OrderState.RECONCILIATION_REQUIRED
    assert result.reason is not None and "illegal transition CREATED -> FILLED" in result.reason
    assert m.current_state(order_id) is OrderState.RECONCILIATION_REQUIRED


def test_reconciliation_state_resolves_to_broker_confirmed_state(
    conn: psycopg.Connection[Any],
) -> None:
    m = _machine(conn)
    order_id = m.create_order(idempotency_key="k-rec")
    m.transition(order_id, OrderState.FILLED)  # flags RECONCILIATION_REQUIRED
    result = m.transition(order_id, OrderState.CANCELED, reason="broker confirmed")
    assert result.applied_as_requested
    assert m.current_state(order_id) is OrderState.CANCELED


def test_duplicate_delivery_is_a_noop(conn: psycopg.Connection[Any]) -> None:
    m = _machine(conn)
    order_id = m.create_order(idempotency_key="k-noop")
    m.transition(order_id, OrderState.VALIDATED)
    before = len(m.events(order_id))
    result = m.transition(order_id, OrderState.VALIDATED)  # duplicate webhook/poll
    assert result.applied_as_requested
    assert len(m.events(order_id)) == before  # no event appended
    assert m.current_state(order_id) is OrderState.VALIDATED


def test_event_log_is_append_only_at_database_level(
    conn: psycopg.Connection[Any],
) -> None:
    m = _machine(conn)
    order_id = m.create_order(idempotency_key="k-ao")
    with pytest.raises(errors.RaiseException, match="append-only"), conn.transaction():
        conn.execute(
            "UPDATE order_events SET new_state = 'FILLED' WHERE order_id = %s",
            (order_id,),
        )
    with pytest.raises(errors.RaiseException, match="append-only"), conn.transaction():
        conn.execute("DELETE FROM order_events WHERE order_id = %s", (order_id,))


def test_cancel_reject_expire_paths(conn: psycopg.Connection[Any]) -> None:
    m = _machine(conn)
    for key, path in {
        "k-cancel": [OrderState.VALIDATED, OrderState.STAGED, OrderState.CANCELED],
        "k-reject": [
            OrderState.VALIDATED,
            OrderState.STAGED,
            OrderState.SUBMITTED,
            OrderState.REJECTED,
        ],
        "k-expire": [
            OrderState.VALIDATED,
            OrderState.AWAITING_APPROVAL,
            OrderState.EXPIRED,
        ],
    }.items():
        order_id = m.create_order(idempotency_key=key)
        for state in path:
            assert m.transition(order_id, state).applied_as_requested, (key, state)


def test_unknown_order_raises(conn: psycopg.Connection[Any]) -> None:
    m = _machine(conn)
    with pytest.raises(LookupError):
        m.current_state(uuid.uuid4())
