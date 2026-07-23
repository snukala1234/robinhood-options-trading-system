"""Token-enforced order submission (Phase F, master-prompt rule 5).

:class:`OrderSubmitter` is the only sanctioned path from an approved proposal
to a broker submit. It requires an :class:`ApprovalToken` — which only the
deterministic trade gate can mint — and re-verifies every binding before
anything happens: expiry, proposal, limit price, quantity, account-state hash,
and quote-snapshot hash. A token minted against one proposal, account state,
or quote snapshot can never be replayed against another.

Audit finding 1 lives here: immediately before calling broker submit, the
submitter re-reads the live kill-switch panel and FAILS CLOSED if any
entry-blocking switch is active or if the current halt epoch differs from the
epoch stamped into the token at issuance. A kill switch tripping between gate
approval and submission — even one tripped and already cleared — invalidates
the token; the proposal must go back through the gate.

Tokens are single-use. Order creation goes through the event-sourced state
machine, so a duplicate idempotency key can still never create two orders.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from src.data.option_chains import ContractQuote
from src.domain.orders import OrderState
from src.domain.values import require_utc
from src.execution.interface import (
    AccountSnapshot,
    BrokerError,
    BrokerInterface,
    LimitOrderRequest,
    LiveOrdersDisabled,
    OrderAck,
    StrategyNotSupported,
)
from src.execution.order_state_machine import OrderStateMachine
from src.gate.kill_switches import KillSwitchPanel
from src.gate.trade_gate import (
    ApprovalToken,
    hash_account_state,
    hash_quote_snapshot,
)


class SubmissionRefused(RuntimeError):
    """The submitter refused to submit. ``reason`` is machine-readable."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}{': ' + detail if detail else ''}")
        self.reason = reason


@dataclass(frozen=True)
class SubmissionReceipt:
    order_id: uuid.UUID
    ack: OrderAck


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class OrderSubmitter:
    """The deterministic execution adapter's entry-order front door."""

    broker: BrokerInterface
    machine: OrderStateMachine
    panel: KillSwitchPanel
    clock: Callable[[], datetime] = _utcnow
    _used_tokens: set[uuid.UUID] = field(default_factory=set, init=False)

    def submit_entry(
        self,
        token: ApprovalToken,
        request: LimitOrderRequest,
        *,
        account: AccountSnapshot,
        leg_quotes: Sequence[ContractQuote],
        quote_snapshot_ids: Sequence[uuid.UUID],
    ) -> SubmissionReceipt:
        """Verify the token, then stage and submit through the state machine."""
        now = require_utc("now", self.clock())

        if not isinstance(token, ApprovalToken):
            raise SubmissionRefused(
                "no_approval_token", "an entry order requires a gate-minted approval token"
            )
        if token.token_id in self._used_tokens:
            raise SubmissionRefused("token_already_used", str(token.token_id))
        if now > token.expires_at:
            raise SubmissionRefused(
                "token_expired", f"expired {token.expires_at.isoformat()}, now {now.isoformat()}"
            )
        if not request.idempotency_key.startswith(f"{token.proposal_id}:"):
            raise SubmissionRefused(
                "token_proposal_mismatch",
                f"token is bound to proposal {token.proposal_id}, "
                f"request key is {request.idempotency_key!r}",
            )
        if request.limit_price != token.limit_price:
            raise SubmissionRefused(
                "limit_price_mismatch",
                f"approved {token.limit_price}, requested {request.limit_price}",
            )
        if request.quantity != token.approved_quantity:
            raise SubmissionRefused(
                "quantity_mismatch",
                f"approved {token.approved_quantity}, requested {request.quantity}",
            )
        if hash_account_state(account) != token.account_state_hash:
            raise SubmissionRefused(
                "account_state_mismatch", "account state changed since gate approval"
            )
        if hash_quote_snapshot(leg_quotes, quote_snapshot_ids) != token.quote_snapshot_hash:
            raise SubmissionRefused(
                "quote_snapshot_mismatch", "quote snapshot changed since gate approval"
            )

        # Order object exists before the final halt check so a refusal at the
        # last instant is auditable as a CANCELED order, not silence.
        order_id = self.machine.create_order(
            idempotency_key=request.idempotency_key,
            proposal_id=token.proposal_id,
            raw_request={
                "underlying": request.underlying,
                "limit_price": str(request.limit_price),
                "quantity": request.quantity,
                "net_intent": request.net_intent.value,
                "token_id": str(token.token_id),
                "correlation_id": str(token.correlation_id),
            },
        )
        self.machine.transition(
            order_id, OrderState.VALIDATED, reason="approval token verified by execution adapter"
        )
        self.machine.transition(order_id, OrderState.STAGED, reason="staged for submission")

        # Audit finding 1: live kill-switch re-read IMMEDIATELY before submit.
        active = self.panel.blocks_new_entries()
        if active:
            self.machine.transition(
                order_id,
                OrderState.CANCELED,
                reason=f"kill switch(es) active at submit: {', '.join(active)}",
            )
            raise SubmissionRefused("kill_switch_active", ", ".join(active))
        if self.panel.halt_epoch != token.halt_epoch:
            self.machine.transition(
                order_id,
                OrderState.CANCELED,
                reason=(
                    f"halt epoch moved since token issuance "
                    f"({token.halt_epoch} -> {self.panel.halt_epoch})"
                ),
            )
            raise SubmissionRefused(
                "halt_epoch_changed",
                f"token epoch {token.halt_epoch}, current {self.panel.halt_epoch}; "
                "the proposal must pass the gate again",
            )

        self._used_tokens.add(token.token_id)  # single-use from this point on
        self.machine.transition(order_id, OrderState.SUBMITTED, reason="submitting to broker")
        try:
            ack = self.broker.submit_order(request)
        except (LiveOrdersDisabled, StrategyNotSupported) as exc:
            # Raised before anything left the process: the order is truthfully dead.
            self.machine.transition(
                order_id, OrderState.REJECTED, reason=f"refused before transport: {exc}"
            )
            raise
        except BrokerError as exc:
            # The call may or may not have reached the broker: state is uncertain.
            self.machine.transition(
                order_id,
                OrderState.RECONCILIATION_REQUIRED,
                reason=f"broker state uncertain after submit attempt: {exc}",
            )
            raise
        self.machine.set_broker_order_id(order_id, ack.broker_order_id)
        self.machine.set_raw_response(order_id, dict(ack.raw))
        self.machine.transition(order_id, ack.state, reason="broker acknowledgment")
        return SubmissionReceipt(order_id=order_id, ack=ack)
