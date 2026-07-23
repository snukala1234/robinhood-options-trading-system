"""Section 10.6 deterministic emergency exits — pure code, NO LLM anywhere.

:func:`emergency_triggers` detects the five emergency conditions from
validated state alone; :class:`EmergencyExitEngine` turns triggers into a
risk-reducing limit order (slippage-aware, high urgency) and submits it
through the token-free exit path. Nothing in this chain touches the model
layer: it works identically when every LLM provider is unreachable.

If the broker lacks the mechanism the exit needs, the submitter alerts and
halts (:class:`~src.execution.submission.ExitMechanismUnavailable`) — the
engine never falls back to legging out.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from psycopg.types.json import Jsonb

from src.config.tunables import DEFAULT_TUNABLES, TunableParams
from src.domain.instruments import LegSide, OptionContract
from src.execution.interface import LimitOrderRequest, NetIntent, OrderLeg
from src.execution.submission import OrderSubmitter, SubmissionReceipt
from src.gate.trade_gate import CircuitBreakerInputs, breached_circuit_breakers
from src.positions.exit_rules import exit_limit_price
from src.positions.monitoring import PositionMarketState


@dataclass(frozen=True)
class EmergencyTrigger:
    name: str
    detail: str


def emergency_triggers(
    state: PositionMarketState,
    *,
    breakers: CircuitBreakerInputs,
    account_equity: Decimal,
    tunables: TunableParams = DEFAULT_TUNABLES,
) -> tuple[EmergencyTrigger, ...]:
    """The five Section 10.6 conditions, each detectable from pure state."""
    triggers: list[EmergencyTrigger] = []

    if state.unrealized_loss_total >= state.position.max_loss:
        triggers.append(
            EmergencyTrigger(
                "max_loss_breach",
                f"unrealized loss {state.unrealized_loss_total} >= defined max loss"
                f" {state.position.max_loss}",
            )
        )

    breached = breached_circuit_breakers(breakers, account_equity)
    if breached:
        triggers.append(EmergencyTrigger("portfolio_drawdown_breach", ", ".join(breached)))

    if state.dte <= tunables.dte_forced_exit:
        triggers.append(
            EmergencyTrigger(
                "dte_expiration_safety",
                f"dte {state.dte} <= forced-exit threshold {tunables.dte_forced_exit}",
            )
        )

    if state.assignment_notice or (
        state.short_leg_itm and state.dte <= tunables.dte_review_checkpoint
    ):
        triggers.append(
            EmergencyTrigger(
                "assignment_exercise_danger",
                "assignment notice received"
                if state.assignment_notice
                else f"short leg ITM with dte {state.dte}",
            )
        )

    if state.state_mismatch:
        triggers.append(
            EmergencyTrigger(
                "position_state_mismatch",
                "broker-reported position disagrees with local state",
            )
        )

    return tuple(triggers)


def build_closing_request(state: PositionMarketState, *, attempt: int = 1) -> LimitOrderRequest:
    """The risk-reducing order that closes the whole position: every leg
    inverted, atomic, day limit, slippage-aware high-urgency price."""
    position = state.position
    legs = tuple(
        OrderLeg(
            contract=OptionContract(
                underlying=position.underlying,
                expiration=position.expiration,
                strike=leg.strike,
                option_type=leg.option_type,
                multiplier=state.multiplier,
            ),
            side=LegSide.SELL if leg.side is LegSide.BUY else LegSide.BUY,
            quantity=leg.quantity,
        )
        for leg in position.legs
    )
    closing_intent = NetIntent.CREDIT if state.opened_intent is NetIntent.DEBIT else NetIntent.DEBIT
    return LimitOrderRequest(
        idempotency_key=f"exit:{position.position_id}:{attempt}",
        underlying=position.underlying,
        legs=legs,
        limit_price=exit_limit_price(
            state.bid, state.ask, opened_intent=state.opened_intent, urgency="high"
        ),
        net_intent=closing_intent,
        quantity=position.quantity,
    )


@dataclass
class EmergencyExitEngine:
    """Detect-and-act, all deterministic: triggers in, limit exit order out."""

    submitter: OrderSubmitter

    def execute(
        self,
        state: PositionMarketState,
        *,
        breakers: CircuitBreakerInputs,
        account_equity: Decimal,
        tunables: TunableParams = DEFAULT_TUNABLES,
        attempt: int = 1,
    ) -> SubmissionReceipt | None:
        """Check the five conditions; when any fires, submit the closing order.
        Returns None when no emergency exists."""
        triggers = emergency_triggers(
            state, breakers=breakers, account_equity=account_equity, tunables=tunables
        )
        if not triggers:
            return None
        self._record_triggers(state, triggers)
        request = build_closing_request(state, attempt=attempt)
        return self.submitter.submit_exit(
            request,
            position_id=state.position.position_id,
            reason="; ".join(t.name for t in triggers),
        )

    def _record_triggers(
        self, state: PositionMarketState, triggers: tuple[EmergencyTrigger, ...]
    ) -> None:
        self.submitter.machine.conn.execute(
            """INSERT INTO system_events
               (id, created_at, severity, component, event_type, correlation_id, payload)
               VALUES (%s, %s, 'critical', 'emergency_exits', 'emergency_exit_triggered', NULL,
                       %s)""",
            (
                uuid.uuid4(),
                datetime.now(UTC),
                Jsonb(
                    {
                        "position_id": str(state.position.position_id),
                        "triggers": [{"name": t.name, "detail": t.detail} for t in triggers],
                        "unrealized_loss": str(state.unrealized_loss_total),
                        "dte": state.dte,
                    }
                ),
            ),
        )
