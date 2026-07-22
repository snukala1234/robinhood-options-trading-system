"""Reconciliation engine (spec 12.1/12.2): local truth vs. broker truth.

The system's core defensive rule (master-prompt rule 14) is implemented here:
**uncertainty blocks new entries.** Any order in RECONCILIATION_REQUIRED, and any
order sitting in SUBMITTED without broker confirmation beyond the timeout, makes
:func:`new_entries_allowed` False until resolved.

The pure decision function :func:`entries_blocked_reasons` is separated from the
database plumbing so it can be property-tested exhaustively.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from src.domain.orders import OrderState, can_transition
from src.domain.values import require_utc
from src.execution.interface import BrokerError, BrokerInterface
from src.execution.order_state_machine import OrderStateMachine
from src.persistence.db import Connection

#: A SUBMITTED order with no broker acknowledgment for this long is uncertain.
SUBMITTED_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class Mismatch:
    order_id: uuid.UUID
    local_state: OrderState
    broker_state: OrderState | None
    action: str  # "advanced" | "flagged" | "missing_at_broker"
    detail: str


@dataclass(frozen=True)
class ReconciliationReport:
    checked: int
    advanced: tuple[Mismatch, ...]
    flagged: tuple[Mismatch, ...]
    missing_at_broker: tuple[uuid.UUID, ...]
    unknown_at_local: tuple[str, ...]  # broker order ids we have no record of
    stale_submitted: tuple[uuid.UUID, ...]

    @property
    def clean(self) -> bool:
        return not (
            self.flagged or self.missing_at_broker or self.unknown_at_local or self.stale_submitted
        )


def entries_blocked_reasons(
    local_states: Sequence[OrderState], stale_submitted_count: int
) -> tuple[str, ...]:
    """Pure decision: why new entries are blocked, empty tuple = allowed.

    Blocked whenever any order needs reconciliation or any submitted order has
    been unacknowledged past the timeout.
    """
    reasons: list[str] = []
    uncertain = sum(1 for s in local_states if s is OrderState.RECONCILIATION_REQUIRED)
    if uncertain:
        reasons.append(f"{uncertain} order(s) in RECONCILIATION_REQUIRED")
    if stale_submitted_count < 0:
        raise ValueError("stale_submitted_count must be >= 0")
    if stale_submitted_count:
        reasons.append(f"{stale_submitted_count} submitted order(s) unacknowledged past timeout")
    return tuple(reasons)


def _stale_submitted(
    machine: OrderStateMachine, now: datetime, timeout_seconds: int
) -> list[uuid.UUID]:
    stale: list[uuid.UUID] = []
    for row in machine.orders_in_state(OrderState.SUBMITTED):
        submitted_at = row["submitted_at"]
        if submitted_at is None:
            stale.append(row["id"])
            continue
        age = (now - submitted_at).total_seconds()
        if age > timeout_seconds:
            stale.append(row["id"])
    return stale


def reconcile(
    machine: OrderStateMachine,
    broker: BrokerInterface,
    *,
    now: datetime,
    conn: Connection,
    submitted_timeout_seconds: int = SUBMITTED_TIMEOUT_SECONDS,
) -> ReconciliationReport:
    """Compare every non-terminal local order against the broker and converge.

    - Broker ahead of us on a legal path -> advance our state (normal progress).
    - Broker reports something illegal from our state -> flag RECONCILIATION_REQUIRED.
    - Broker doesn't know an order we sent -> flag; state is uncertain.
    - Broker has an open order we don't know -> report + system event (severity
      critical: it means something submitted outside the state machine).
    - SUBMITTED beyond timeout without acknowledgment -> flag as stale.
    """
    now = require_utc("now", now)
    advanced: list[Mismatch] = []
    flagged: list[Mismatch] = []
    missing: list[uuid.UUID] = []

    local_orders = machine.non_terminal_orders()
    known_broker_ids: set[str] = set()

    for row in local_orders:
        order_id = row["id"]
        broker_order_id = row["broker_order_id"]
        local_state = OrderState(str(row["current_state"]))
        if broker_order_id is None:
            continue  # pre-submission states have nothing to compare yet
        known_broker_ids.add(str(broker_order_id))
        try:
            status = broker.order_status(str(broker_order_id))
        except BrokerError as exc:
            result = machine.transition(
                order_id,
                OrderState.RECONCILIATION_REQUIRED,
                reason=f"broker does not know order: {exc}",
            )
            missing.append(order_id)
            flagged.append(Mismatch(order_id, local_state, None, "missing_at_broker", str(exc)))
            _system_event(
                conn,
                severity="critical",
                event_type="order_missing_at_broker",
                payload={"order_id": str(order_id), "state": result.new_state.value},
            )
            continue

        if status.state is local_state:
            continue
        if can_transition(local_state, status.state):
            machine.transition(
                order_id,
                status.state,
                broker_payload=dict(status.raw),
                reason="reconciliation: broker state applied",
            )
            advanced.append(
                Mismatch(order_id, local_state, status.state, "advanced", "broker ahead")
            )
        else:
            machine.transition(
                order_id,
                status.state,  # illegal -> machine flags RECONCILIATION_REQUIRED
                broker_payload=dict(status.raw),
                reason="reconciliation: broker reports illegal transition",
            )
            flagged.append(
                Mismatch(
                    order_id,
                    local_state,
                    status.state,
                    "flagged",
                    f"broker {status.state.value} unreachable from {local_state.value}",
                )
            )
            _system_event(
                conn,
                severity="critical",
                event_type="order_state_mismatch",
                payload={
                    "order_id": str(order_id),
                    "local_state": local_state.value,
                    "broker_state": status.state.value,
                },
            )

    unknown_at_local: list[str] = []
    try:
        broker_open = broker.open_orders()
    except BrokerError:
        broker_open = ()
    for status in broker_open:
        if status.broker_order_id not in known_broker_ids:
            unknown_at_local.append(status.broker_order_id)
            _system_event(
                conn,
                severity="critical",
                event_type="unknown_broker_order",
                payload={"broker_order_id": status.broker_order_id},
            )

    stale = _stale_submitted(machine, now, submitted_timeout_seconds)
    for order_id in stale:
        machine.transition(
            order_id,
            OrderState.RECONCILIATION_REQUIRED,
            reason=f"submitted unacknowledged for > {submitted_timeout_seconds}s",
        )

    return ReconciliationReport(
        checked=len(local_orders),
        advanced=tuple(advanced),
        flagged=tuple(flagged),
        missing_at_broker=tuple(missing),
        unknown_at_local=tuple(unknown_at_local),
        stale_submitted=tuple(stale),
    )


def new_entries_allowed(
    machine: OrderStateMachine,
    *,
    now: datetime,
    submitted_timeout_seconds: int = SUBMITTED_TIMEOUT_SECONDS,
) -> tuple[bool, tuple[str, ...]]:
    """Master-prompt rule 14: uncertainty in order state blocks new entries."""
    now = require_utc("now", now)
    states = [
        OrderState(str(r["current_state"]))
        for r in machine.orders_in_state(OrderState.RECONCILIATION_REQUIRED)
    ]
    stale = _stale_submitted(machine, now, submitted_timeout_seconds)
    reasons = entries_blocked_reasons(states, len(stale))
    return (not reasons, reasons)


def _system_event(
    conn: Connection, *, severity: str, event_type: str, payload: dict[str, Any]
) -> None:
    conn.execute(
        """INSERT INTO system_events
           (id, created_at, severity, component, event_type, correlation_id, payload)
           VALUES (%s, %s, %s, 'reconciliation', %s, NULL, %s)""",
        (uuid.uuid4(), datetime.now(UTC), severity, event_type, Jsonb(payload)),
    )
