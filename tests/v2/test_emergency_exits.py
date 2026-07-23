"""Section 10.6: emergency exits fire from pure code, no model layer anywhere."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

from src.domain.instruments import LegSide
from src.execution.interface import NetIntent
from src.execution.order_state_machine import OrderStateMachine
from src.execution.paper_broker import PaperBroker
from src.execution.submission import OrderSubmitter
from src.gate.kill_switches import KillSwitchPanel
from src.gate.trade_gate import CircuitBreakerInputs
from src.positions.emergency import (
    EmergencyExitEngine,
    build_closing_request,
    emergency_triggers,
)
from tests.v2.gate_harness import NOW
from tests.v2.position_harness import make_spread_position, make_state

D = Decimal
SRC = Path(__file__).resolve().parents[2] / "src"
EQUITY = D("100000")
NO_BREACH = CircuitBreakerInputs(D("0"), D("0"), D("0"), D("0"))


def _names(state: Any, breakers: CircuitBreakerInputs = NO_BREACH) -> set[str]:
    return {t.name for t in emergency_triggers(state, breakers=breakers, account_equity=EQUITY)}


# --- the five Section 10.6 triggers -------------------------------------------


def test_healthy_position_has_no_emergency() -> None:
    assert _names(make_state()) == set()


def test_max_loss_breach_triggers() -> None:
    # Mark collapsed to zero: loss = 4.50 x 100 x 2 = 900 >= defined max loss 900.
    state = make_state(current_net_price=D("0"), bid=D("0"), ask=D("0.05"))
    assert "max_loss_breach" in _names(state)


def test_portfolio_drawdown_breach_triggers() -> None:
    breakers = CircuitBreakerInputs(D("2500"), D("0"), D("0"), D("0"))
    assert "portfolio_drawdown_breach" in _names(make_state(), breakers)


def test_dte_expiration_safety_triggers() -> None:
    assert "dte_expiration_safety" in _names(make_state(dte=2))


def test_assignment_danger_triggers() -> None:
    assert "assignment_exercise_danger" in _names(make_state(assignment_notice=True))
    assert "assignment_exercise_danger" in _names(make_state(short_leg_itm=True, dte=5))
    # A short leg ITM far from expiration is not yet an emergency.
    assert "assignment_exercise_danger" not in _names(make_state(short_leg_itm=True, dte=16))


def test_position_state_mismatch_triggers() -> None:
    assert "position_state_mismatch" in _names(make_state(state_mismatch=True))


# --- the closing order is deterministic and risk-reducing ----------------------


def test_closing_request_inverts_every_leg_atomically() -> None:
    state = make_state(
        position=make_spread_position(),
        current_net_price=D("2.00"),
        bid=D("1.90"),
        ask=D("2.10"),
    )
    request = build_closing_request(state)
    assert request.is_multi_leg
    assert [leg.side for leg in request.legs] == [LegSide.SELL, LegSide.BUY]
    assert request.net_intent is NetIntent.CREDIT  # closing a debit structure
    assert request.quantity == 1
    assert request.idempotency_key == f"exit:{state.position.position_id}:1"
    # High-urgency slippage-aware limit: mid 2.00 minus half the 0.20 spread.
    assert request.limit_price == D("1.90")


# --- pure-code integration: model layer entirely absent ------------------------


def test_emergency_exit_fires_end_to_end_with_no_llm(
    conn: psycopg.Connection[Any],
) -> None:
    """dte hits the safety threshold -> detection, order build, submission, and
    the full Section 12.2 walk all happen in pure code. No agent, no model
    provider, no prompt exists anywhere in this test."""
    panel = KillSwitchPanel()
    machine = OrderStateMachine(conn)
    submitter = OrderSubmitter(
        broker=PaperBroker(starting_cash=D("50000"), clock=lambda: NOW),
        machine=machine,
        panel=panel,
        clock=lambda: NOW,
    )
    engine = EmergencyExitEngine(submitter=submitter)

    receipt = engine.execute(make_state(dte=1), breakers=NO_BREACH, account_equity=EQUITY)
    assert receipt is not None
    assert receipt.ack.state.value == "OPEN"
    states = [str(e["new_state"]) for e in machine.events(receipt.order_id)]
    assert states == ["CREATED", "VALIDATED", "STAGED", "SUBMITTED", "OPEN"]
    row = machine.get_order(receipt.order_id)
    assert row is not None
    assert row["raw_request"]["risk_reducing_exit"] is True
    assert "dte_expiration_safety" in row["raw_request"]["reason"]

    event = conn.execute(
        "SELECT * FROM system_events WHERE event_type = 'emergency_exit_triggered'"
    ).fetchone()
    assert event is not None and event["severity"] == "critical"
    assert event["payload"]["triggers"][0]["name"] == "dte_expiration_safety"


def test_no_emergency_means_no_order_and_no_events(conn: psycopg.Connection[Any]) -> None:
    submitter = OrderSubmitter(
        broker=PaperBroker(starting_cash=D("50000"), clock=lambda: NOW),
        machine=OrderStateMachine(conn),
        panel=KillSwitchPanel(),
        clock=lambda: NOW,
    )
    receipt = EmergencyExitEngine(submitter=submitter).execute(
        make_state(), breakers=NO_BREACH, account_equity=EQUITY
    )
    assert receipt is None
    assert conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"] == 0  # type: ignore[index]
    assert conn.execute("SELECT COUNT(*) AS n FROM system_events").fetchone()["n"] == 0  # type: ignore[index]


def test_positions_package_never_imports_the_model_layer() -> None:
    """Structural proof of 'no LLM dependency': nothing under src/positions
    imports the agents package or any model SDK."""
    offenders: list[str] = []
    for path in (SRC / "positions").rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            if "src.agents" in stripped or "anthropic" in stripped.lower():
                offenders.append(f"{path.name}: {stripped}")
    assert offenders == []
