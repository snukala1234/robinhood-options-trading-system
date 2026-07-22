"""Property tests (hypothesis) for the Phase D non-negotiables."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import psycopg
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from psycopg.rows import dict_row

from src.domain.instruments import LegSide, OptionContract, OptionType
from src.domain.orders import ALLOWED_TRANSITIONS, OrderState, can_transition
from src.execution.interface import (
    DuplicateOrderError,
    LimitOrderRequest,
    NetIntent,
    OrderLeg,
)
from src.execution.order_state_machine import OrderStateMachine
from src.execution.paper_broker import PaperBroker
from src.execution.reconciliation import entries_blocked_reasons

D = Decimal
NOW = datetime(2026, 7, 22, 15, 0, tzinfo=UTC)

states = st.sampled_from(list(OrderState))
keys = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=8, max_size=40)


@given(current=states, requested=states)
def test_can_transition_agrees_with_table_exactly(
    current: OrderState, requested: OrderState
) -> None:
    assert can_transition(current, requested) == (requested in ALLOWED_TRANSITIONS[current])


@given(local_states=st.lists(states, max_size=12), stale=st.integers(0, 5))
def test_uncertainty_always_blocks_new_entries(local_states: list[OrderState], stale: int) -> None:
    """New entries are allowed iff NO order needs reconciliation AND nothing is
    stale — for every possible combination of order states."""
    reasons = entries_blocked_reasons(local_states, stale)
    has_uncertainty = (
        any(s is OrderState.RECONCILIATION_REQUIRED for s in local_states) or stale > 0
    )
    assert bool(reasons) == has_uncertainty


@given(key=keys, attempts=st.integers(2, 5))
@settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_duplicate_idempotency_key_never_creates_two_orders(
    migrated: str, key: str, attempts: int
) -> None:
    """DB-backed property: however many times a key is replayed, exactly one order
    row exists and every replay raises DuplicateOrderError."""
    with psycopg.connect(migrated, row_factory=dict_row) as conn:
        try:
            m = OrderStateMachine(conn)
            unique_key = f"prop-{key}"
            first = m.create_order(idempotency_key=unique_key)
            for _ in range(attempts - 1):
                with pytest.raises(DuplicateOrderError) as exc:
                    m.create_order(idempotency_key=unique_key)
                assert exc.value.existing_id == str(first)
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM orders WHERE idempotency_key = %s",
                (unique_key,),
            ).fetchone()
            assert row is not None and row["n"] == 1
        finally:
            conn.rollback()


@given(key=keys, attempts=st.integers(2, 6))
def test_paper_broker_idempotent_replay_property(key: str, attempts: int) -> None:
    """Broker-level property: replaying a key N times yields one order, same id."""
    broker = PaperBroker(starting_cash=D("1000"), clock=lambda: NOW)
    contract = OptionContract("SPY", date(2026, 8, 7), D("600"), OptionType.CALL)
    request = LimitOrderRequest(
        idempotency_key=key,
        underlying="SPY",
        legs=(OrderLeg(contract, LegSide.BUY, 1),),
        limit_price=D("2.50"),
        net_intent=NetIntent.DEBIT,
        quantity=1,
    )
    acks = [broker.submit_order(request) for _ in range(attempts)]
    assert len({a.broker_order_id for a in acks}) == 1
    assert len(broker.open_orders()) == 1
