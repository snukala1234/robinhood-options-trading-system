"""Degraded mode: entries blocked, risk-reducing exits still possible — and
when the broker cannot reduce risk, the system alerts and halts."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg
import pytest

from src.domain.orders import OrderState
from src.execution.capabilities import BrokerCapabilities
from src.execution.order_state_machine import OrderStateMachine
from src.execution.paper_broker import PaperBroker
from src.execution.submission import (
    ExitMechanismUnavailable,
    OrderSubmitter,
    SubmissionRefused,
)
from src.gate.kill_switches import KillSwitchPanel
from src.gate.trade_gate import TradeGate
from src.positions.degraded import degraded_mode_status
from src.positions.emergency import build_closing_request
from src.risk.settlement import CashAccountState
from tests.v2.gate_harness import NOW, make_input
from tests.v2.position_harness import make_spread_position, make_state

D = Decimal


def _submitter(
    conn: psycopg.Connection[Any],
    panel: KillSwitchPanel,
    caps: BrokerCapabilities | None = None,
) -> tuple[OrderSubmitter, OrderStateMachine]:
    machine = OrderStateMachine(conn)
    broker = PaperBroker(starting_cash=D("50000"), clock=lambda: NOW)
    if caps is not None:
        broker.caps = caps
    return (
        OrderSubmitter(broker=broker, machine=machine, panel=panel, clock=lambda: NOW),
        machine,
    )


def _exit_request(state: Any) -> Any:
    return build_closing_request(state)


def test_reconciliation_uncertainty_degrades_entries_but_not_exits(
    conn: psycopg.Connection[Any],
) -> None:
    panel = KillSwitchPanel()
    machine = OrderStateMachine(conn)
    # Force an uncertain order: an illegal transition flags RECONCILIATION_REQUIRED.
    order_id = machine.create_order(idempotency_key="stray:1")
    machine.transition(order_id, OrderState.OPEN, reason="broker claims open from CREATED")

    status = degraded_mode_status(machine, panel, now=NOW)
    assert status.degraded and not status.new_entries_allowed
    assert any("RECONCILIATION_REQUIRED" in r for r in status.entry_block_reasons)
    assert status.exits_allowed and status.exit_block_reasons == ()


def test_degraded_mode_blocks_entry_but_permits_risk_reducing_exit(
    conn: psycopg.Connection[Any],
) -> None:
    """The requested end-to-end proof: same session, same panel — a new entry
    dies at the gate while a risk-reducing exit goes straight through."""
    panel = KillSwitchPanel()
    panel.activate("new_entry_halt", reason="degraded: allow exits only")

    # Entry: rejected at step 1 by the active switch — no token exists.
    gate_result = TradeGate(panel=panel, clock=lambda: NOW).evaluate(make_input())
    assert not gate_result.approved and gate_result.token is None
    assert gate_result.rejection_step == "system_health_and_data_freshness"
    assert any("new_entry_halt" in r for r in gate_result.reasons)

    # Exit: token-free risk-reducing path succeeds under the same halt.
    submitter, machine = _submitter(conn, panel)
    state = make_state(
        position=make_spread_position(),
        current_net_price=D("2.00"),
        bid=D("1.90"),
        ask=D("2.10"),
    )
    receipt = submitter.submit_exit(
        _exit_request(state),
        position_id=state.position.position_id,
        reason="risk_reducing_exit_under_degraded_mode",
        cash_state=CashAccountState(settled_cash=D("0")),  # settlement never blocks exits
    )
    assert receipt.ack.state.value == "OPEN"
    states = [str(e["new_state"]) for e in machine.events(receipt.order_id)]
    assert states == ["CREATED", "VALIDATED", "STAGED", "SUBMITTED", "OPEN"]

    status = degraded_mode_status(machine, panel, now=NOW)
    assert not status.new_entries_allowed and status.exits_allowed


def test_global_halt_blocks_exits_too(conn: psycopg.Connection[Any]) -> None:
    panel = KillSwitchPanel()
    panel.activate("global_trading_halt", reason="test")
    submitter, _ = _submitter(conn, panel)
    state = make_state()
    with pytest.raises(SubmissionRefused) as exc_info:
        submitter.submit_exit(
            _exit_request(state),
            position_id=state.position.position_id,
            reason="test",
        )
    assert exc_info.value.reason == "exits_halted"
    status = degraded_mode_status(OrderStateMachine(conn), panel, now=NOW)
    assert not status.exits_allowed


def test_missing_exit_mechanism_alerts_and_halts_instead_of_improvising(
    conn: psycopg.Connection[Any],
) -> None:
    """Spec 10.6: a spread must be closed atomically. On a single-leg-only
    account the system must NOT leg out — it alerts, trips broker_degradation,
    and submits nothing at all."""
    panel = KillSwitchPanel()
    single_leg_only = BrokerCapabilities(
        account_read=True,
        single_leg_orders=True,
        limit_orders=True,
        cancel_supported=True,
        price_increment=D("0.01"),
    )
    submitter, machine = _submitter(conn, panel, caps=single_leg_only)
    state = make_state(
        position=make_spread_position(),
        current_net_price=D("2.00"),
        bid=D("1.90"),
        ask=D("2.10"),
    )

    with pytest.raises(ExitMechanismUnavailable, match="never legs out"):
        submitter.submit_exit(
            _exit_request(state),
            position_id=state.position.position_id,
            reason="spread_close_on_restricted_account",
        )

    # Alerted: critical system event with the missing mechanism named.
    event = conn.execute(
        "SELECT * FROM system_events WHERE event_type = 'exit_mechanism_unavailable'"
    ).fetchone()
    assert event is not None and event["severity"] == "critical"
    assert event["payload"]["missing"] == "atomic multi-leg close"

    # Halted: broker_degradation tripped (epoch bumped), so nothing else moves.
    assert panel.is_active("broker_degradation")
    assert panel.halt_epoch == 1

    # Nothing was submitted, staged, or legged out: zero orders anywhere.
    assert conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"] == 0  # type: ignore[index]
    assert submitter.broker.open_orders() == ()

    # And the halt is real: even a single-leg exit is now refused.
    single_state = make_state()
    with pytest.raises(SubmissionRefused) as exc_info:
        submitter.submit_exit(
            build_closing_request(single_state),
            position_id=single_state.position.position_id,
            reason="post_halt_attempt",
        )
    assert exc_info.value.reason == "exits_halted"
    assert machine.non_terminal_orders() == []
