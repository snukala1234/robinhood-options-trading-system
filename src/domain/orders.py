"""Order intents and the Section 12.2 order state machine (states + legal transitions).

The state *machine engine* (event-sourced persistence, reconciliation) is Phase D;
this module defines the domain vocabulary it will enforce: the states, which
transitions are legal, and the order-intent value object with its idempotency key.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from src.domain.values import (
    DomainValidationError,
    require_positive_int,
    require_positive_money,
)


class OrderState(StrEnum):
    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    STAGED = "STAGED"
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


#: States from which no further transition is legal (except into reconciliation,
#: which is always reachable — an uncertain terminal state must be investigable).
TERMINAL_STATES = frozenset(
    {OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED, OrderState.EXPIRED}
)

_R = OrderState.RECONCILIATION_REQUIRED

#: Legal transitions (Section 12.2). Anything not listed is illegal and must be
#: recorded as RECONCILIATION_REQUIRED by the Phase D engine, never silently applied.
ALLOWED_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.CREATED: frozenset({OrderState.VALIDATED, OrderState.REJECTED, _R}),
    OrderState.VALIDATED: frozenset(
        {OrderState.AWAITING_APPROVAL, OrderState.STAGED, OrderState.REJECTED, _R}
    ),
    OrderState.AWAITING_APPROVAL: frozenset(
        {OrderState.STAGED, OrderState.CANCELED, OrderState.EXPIRED, _R}
    ),
    OrderState.STAGED: frozenset({OrderState.SUBMITTED, OrderState.CANCELED, _R}),
    OrderState.SUBMITTED: frozenset(
        {OrderState.OPEN, OrderState.REJECTED, OrderState.CANCELED, _R}
    ),
    OrderState.OPEN: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.EXPIRED,
            _R,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {OrderState.FILLED, OrderState.CANCELED, OrderState.EXPIRED, _R}
    ),
    OrderState.FILLED: frozenset({_R}),
    OrderState.CANCELED: frozenset({_R}),
    OrderState.REJECTED: frozenset({_R}),
    OrderState.EXPIRED: frozenset({_R}),
    # Reconciliation resolves only into a state confirmed against the broker.
    _R: frozenset(
        {
            OrderState.OPEN,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        }
    ),
}


def can_transition(current: OrderState, new: OrderState) -> bool:
    """True iff ``current -> new`` is a legal Section 12.2 transition."""
    return new in ALLOWED_TRANSITIONS[current]


@dataclass(frozen=True)
class OrderIntent:
    """An approved order intent bound to a proposal, carrying its idempotency key.

    The idempotency key is deterministic per (proposal, attempt) so the same intent
    can never create two broker orders (Section 12.1); the Phase D adapter enforces
    uniqueness against the ``orders.idempotency_key`` UNIQUE constraint.
    """

    proposal_id: uuid.UUID
    limit_price: Decimal
    quantity: int
    attempt: int = 1
    intent_id: uuid.UUID = field(default_factory=uuid.uuid4)

    def __post_init__(self) -> None:
        if not isinstance(self.proposal_id, uuid.UUID):
            raise DomainValidationError("proposal_id must be a UUID")
        require_positive_money("limit_price", self.limit_price)
        require_positive_int("quantity", self.quantity)
        require_positive_int("attempt", self.attempt)

    @property
    def idempotency_key(self) -> str:
        return f"{self.proposal_id}:{self.attempt}"
