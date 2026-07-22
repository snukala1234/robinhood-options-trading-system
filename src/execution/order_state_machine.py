"""Idempotent, event-sourced order state machine (spec 12.2) over PostgreSQL.

Truth lives in ``order_events`` (append-only, trigger-protected); ``orders`` holds
a synchronized current-state pointer. Two invariants are enforced twice each:

- **One idempotency key, one order.** Checked in code and by the database UNIQUE
  constraint — a race that slips past the code check dies on the constraint.
- **Only legal Section 12.2 transitions apply.** An illegal transition request is
  never applied; it is recorded as a transition to ``RECONCILIATION_REQUIRED``
  with the attempted move in the reason, which blocks new entries until a human
  or the reconciliation engine resolves it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from psycopg import errors
from psycopg.types.json import Jsonb

from src.domain.orders import OrderState, can_transition
from src.execution.interface import DuplicateOrderError
from src.persistence.db import Connection


@dataclass(frozen=True)
class TransitionResult:
    order_id: uuid.UUID
    previous_state: OrderState
    new_state: OrderState
    applied_as_requested: bool  # False when flagged to RECONCILIATION_REQUIRED
    reason: str | None


@dataclass(frozen=True)
class OrderStateMachine:
    conn: Connection

    # -- creation ----------------------------------------------------------

    def create_order(
        self,
        *,
        idempotency_key: str,
        proposal_id: uuid.UUID | None = None,
        raw_request: dict[str, Any] | None = None,
    ) -> uuid.UUID:
        """Create an order in CREATED state. A reused idempotency key raises
        :class:`DuplicateOrderError` — the same intent can never create two orders."""
        existing = self.order_by_idempotency_key(idempotency_key)
        if existing is not None:
            raise DuplicateOrderError(idempotency_key, str(existing["id"]))
        order_id = uuid.uuid4()
        try:
            self.conn.execute(
                """INSERT INTO orders
                   (id, proposal_id, idempotency_key, broker_order_id, current_state,
                    submitted_at, raw_request, raw_response)
                   VALUES (%s, %s, %s, NULL, %s, NULL, %s, NULL)""",
                (
                    order_id,
                    proposal_id,
                    idempotency_key,
                    OrderState.CREATED.value,
                    Jsonb(raw_request) if raw_request is not None else None,
                ),
            )
        except errors.UniqueViolation as exc:
            # Race lost to a concurrent writer: the constraint is the backstop.
            raise DuplicateOrderError(idempotency_key, "concurrent-insert") from exc
        self._append_event(order_id, None, OrderState.CREATED, None, "order created")
        return order_id

    # -- state -------------------------------------------------------------

    def current_state(self, order_id: uuid.UUID) -> OrderState:
        """Current state derived from the append-only event log (the truth)."""
        cur = self.conn.execute(
            "SELECT new_state FROM order_events WHERE order_id = %s "
            "ORDER BY event_at DESC, id DESC LIMIT 1",
            (order_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"unknown order {order_id}")
        return OrderState(str(row["new_state"]))

    def transition(
        self,
        order_id: uuid.UUID,
        new_state: OrderState,
        *,
        broker_payload: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> TransitionResult:
        """Apply a legal transition, or record RECONCILIATION_REQUIRED for an
        illegal one. Repeating the current state is an idempotent no-op (duplicate
        broker poll/webhook deliveries must not corrupt the log)."""
        current = self.current_state(order_id)
        if new_state is current:
            return TransitionResult(order_id, current, current, True, "duplicate delivery no-op")
        if can_transition(current, new_state):
            self._append_event(order_id, current, new_state, broker_payload, reason)
            if new_state is OrderState.SUBMITTED:
                self.conn.execute(
                    "UPDATE orders SET submitted_at = %s WHERE id = %s",
                    (datetime.now(UTC), order_id),
                )
            return TransitionResult(order_id, current, new_state, True, reason)
        flag_reason = f"illegal transition {current.value} -> {new_state.value}" + (
            f" ({reason})" if reason else ""
        )
        self._append_event(
            order_id, current, OrderState.RECONCILIATION_REQUIRED, broker_payload, flag_reason
        )
        return TransitionResult(
            order_id, current, OrderState.RECONCILIATION_REQUIRED, False, flag_reason
        )

    def set_broker_order_id(self, order_id: uuid.UUID, broker_order_id: str) -> None:
        self.conn.execute(
            "UPDATE orders SET broker_order_id = %s WHERE id = %s",
            (broker_order_id, order_id),
        )

    def set_raw_response(self, order_id: uuid.UUID, raw_response: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE orders SET raw_response = %s WHERE id = %s",
            (Jsonb(raw_response), order_id),
        )

    # -- queries -----------------------------------------------------------

    def order_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        cur = self.conn.execute("SELECT * FROM orders WHERE idempotency_key = %s", (key,))
        return cur.fetchone()

    def get_order(self, order_id: uuid.UUID) -> dict[str, Any] | None:
        cur = self.conn.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
        return cur.fetchone()

    def events(self, order_id: uuid.UUID) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM order_events WHERE order_id = %s ORDER BY event_at, id",
            (order_id,),
        )
        return cur.fetchall()

    def orders_in_state(self, state: OrderState) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM orders WHERE current_state = %s ORDER BY id",
            (state.value,),
        )
        return cur.fetchall()

    def non_terminal_orders(self) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM orders WHERE current_state NOT IN "
            "('FILLED', 'CANCELED', 'REJECTED', 'EXPIRED') ORDER BY id"
        )
        return cur.fetchall()

    # -- internal ----------------------------------------------------------

    def _append_event(
        self,
        order_id: uuid.UUID,
        previous: OrderState | None,
        new_state: OrderState,
        broker_payload: dict[str, Any] | None,
        reason: str | None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO order_events
               (id, order_id, event_at, previous_state, new_state, broker_payload,
                reason)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                uuid.uuid4(),
                order_id,
                datetime.now(UTC),
                previous.value if previous is not None else None,
                new_state.value,
                Jsonb(broker_payload) if broker_payload is not None else None,
                reason,
            ),
        )
        self.conn.execute(
            "UPDATE orders SET current_state = %s WHERE id = %s",
            (new_state.value, order_id),
        )
