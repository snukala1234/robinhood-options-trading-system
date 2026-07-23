"""Section 15.1 startup validation: ten checks, fail closed, restart recovery."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from src.data.option_chains import ContractQuote
from src.domain.instruments import LegSide
from src.domain.orders import OrderState
from src.execution.interface import (
    BrokerUnavailable,
    LimitOrderRequest,
    NetIntent,
    OrderLeg,
)
from src.execution.order_state_machine import OrderStateMachine
from src.execution.paper_broker import PaperBroker
from src.gate.kill_switches import KillSwitchPanel
from src.orchestration.config_integrity import stamped_evidence
from src.orchestration.session import SessionMachine, SessionState, SessionTransitionError
from src.orchestration.startup import StartupValidator
from src.persistence.repositories import ConfigVersionRepository
from tests.v2.gate_harness import CALL_600, make_quote

D = Decimal

EXPECTED_CHECKS = [
    "market_calendar_and_session",
    "clock_synchronization",
    "database_health",
    "broker_authentication",
    "capability_snapshot_refresh",
    "reconcile_account_positions_orders",
    "market_data_feed",
    "active_config_integrity",
    "paper_live_confirmation",
    "kill_switch_self_test",
]


def _fresh_quote() -> ContractQuote:
    now = datetime.now(UTC)
    return make_quote(observed_at=now, received_at=now)


def _seed_active_config(conn: psycopg.Connection[Any]) -> None:
    repo = ConfigVersionRepository(conn)
    parameters = {"profit_target_pct_of_max_gain": 0.5, "dte_forced_exit": 2}
    version_id = repo.insert_version(
        parameters, status="shadow", evidence=stamped_evidence(parameters)
    )
    repo.transition(version_id, "active", approved_by="human-operator")


def _validator(
    conn: psycopg.Connection[Any],
    broker: Any = None,
    panel: KillSwitchPanel | None = None,
) -> StartupValidator:
    return StartupValidator(
        conn=conn,
        broker=broker or PaperBroker(starting_cash=D("50000")),
        panel=panel or KillSwitchPanel(),
        market_data_probe=_fresh_quote,
        account_id_hash="test-account-hash",
    )


class _DeadBroker:
    """A broker whose every probe fails — the truthful state is unknown."""

    def capabilities(self) -> Any:
        raise BrokerUnavailable("no transport connected")

    def account_snapshot(self) -> Any:
        raise BrokerUnavailable("no transport connected")

    def positions(self) -> Any:
        raise BrokerUnavailable("no transport connected")

    def open_orders(self) -> Any:
        raise BrokerUnavailable("no transport connected")

    def order_status(self, broker_order_id: str) -> Any:
        raise BrokerUnavailable("no transport connected")

    def preview_order(self, request: Any) -> Any:
        raise BrokerUnavailable("no transport connected")

    def submit_order(self, request: Any) -> Any:
        raise BrokerUnavailable("no transport connected")

    def cancel_order(self, broker_order_id: str) -> Any:
        raise BrokerUnavailable("no transport connected")


def test_all_green_run_passes_all_ten_checks(conn: psycopg.Connection[Any]) -> None:
    _seed_active_config(conn)
    panel = KillSwitchPanel()
    report = _validator(conn, panel=panel).run()
    assert [c.name for c in report.checks] == EXPECTED_CHECKS
    assert report.passed, report.blocking_reasons
    # Paper/live state is confirmed visibly.
    assert "MODE: PAPER" in report.mode_banner
    assert "ORDER_MODE=research_only" in report.mode_banner
    assert "live orders possible: False" in report.mode_banner
    # The kill-switch self-test really exercised the panel and left it clean.
    assert panel.halt_epoch == 2 and panel.active_switches() == ()
    # The report is journaled.
    row = conn.execute(
        "SELECT * FROM system_events WHERE event_type = 'startup_validation'"
    ).fetchone()
    assert row is not None and row["severity"] == "info"
    assert row["payload"]["passed"] is True

    machine = SessionMachine(conn=conn)
    machine.begin_session()
    assert machine.complete_startup(report) is SessionState.PREMARKET_RESEARCH


def test_failed_check_blocks_the_session(conn: psycopg.Connection[Any]) -> None:
    _seed_active_config(conn)
    report = _validator(conn, broker=_DeadBroker()).run()
    assert not report.passed
    failed = {c.name for c in report.checks if not c.passed}
    assert "broker_authentication" in failed
    machine = SessionMachine(conn=conn)
    machine.begin_session()
    with pytest.raises(SessionTransitionError, match="remains in STARTUP_VALIDATION"):
        machine.complete_startup(report)
    assert machine.state is SessionState.STARTUP_VALIDATION


def test_missing_active_config_blocks(conn: psycopg.Connection[Any]) -> None:
    report = _validator(conn).run()
    check = next(c for c in report.checks if c.name == "active_config_integrity")
    assert not check.passed and "no active config" in check.detail
    assert not report.passed


def test_tampered_config_hash_blocks(conn: psycopg.Connection[Any]) -> None:
    repo = ConfigVersionRepository(conn)
    parameters = {"dte_forced_exit": 2}
    version_id = repo.insert_version(
        parameters,
        status="shadow",
        evidence=stamped_evidence({"dte_forced_exit": 4}),  # hash of DIFFERENT params
    )
    repo.transition(version_id, "active", approved_by="human-operator")
    report = _validator(conn).run()
    check = next(c for c in report.checks if c.name == "active_config_integrity")
    assert not check.passed and "mismatch" in check.detail


def test_unclean_reconciliation_blocks(conn: psycopg.Connection[Any]) -> None:
    _seed_active_config(conn)
    machine = OrderStateMachine(conn)
    # A locally SUBMITTED order the broker has never heard of: uncertain state.
    order_id = machine.create_order(idempotency_key="ghost:1")
    machine.transition(order_id, OrderState.VALIDATED)
    machine.transition(order_id, OrderState.STAGED)
    machine.transition(order_id, OrderState.SUBMITTED)
    machine.set_broker_order_id(order_id, "P-999999")
    report = _validator(conn).run()
    check = next(c for c in report.checks if c.name == "reconcile_account_positions_orders")
    assert not check.passed and "missing at broker" in check.detail
    assert not report.passed


def test_stale_market_data_blocks(conn: psycopg.Connection[Any]) -> None:
    _seed_active_config(conn)
    stale = make_quote()  # observed at the fixed harness time, far from real now
    validator = StartupValidator(
        conn=conn,
        broker=PaperBroker(starting_cash=D("50000")),
        panel=KillSwitchPanel(),
        market_data_probe=lambda: stale,
        account_id_hash="test-account-hash",
    )
    report = validator.run()
    check = next(c for c in report.checks if c.name == "market_data_feed")
    assert not check.passed
    assert not report.passed


def test_restart_with_open_positions_recovers_state(conn: psycopg.Connection[Any]) -> None:
    """A mid-session restart: broker holds a live partially-filled order and a
    position. Startup reconciliation converges local state to broker truth and
    the session resumes into POSITION_MANAGEMENT, not a fresh morning."""
    _seed_active_config(conn)
    broker = PaperBroker(starting_cash=D("50000"))
    request = LimitOrderRequest(
        idempotency_key="restart:1",
        underlying="SPY",
        legs=(OrderLeg(contract=CALL_600, side=LegSide.BUY, quantity=1),),
        limit_price=D("4.50"),
        net_intent=NetIntent.DEBIT,
        quantity=2,
    )
    ack = broker.submit_order(request)
    broker.fill(ack.broker_order_id, 1, D("4.45"))  # partial fill -> open position

    # Local truth as persisted before the "crash": order known, still OPEN.
    machine = OrderStateMachine(conn)
    order_id = machine.create_order(idempotency_key="restart:1")
    machine.transition(order_id, OrderState.VALIDATED)
    machine.transition(order_id, OrderState.STAGED)
    machine.transition(order_id, OrderState.SUBMITTED)
    machine.set_broker_order_id(order_id, ack.broker_order_id)
    machine.transition(order_id, OrderState.OPEN)

    report = _validator(conn, broker=broker).run()
    assert report.passed, report.blocking_reasons
    assert report.broker_positions == 1
    assert report.open_orders == 1

    # Reconciliation advanced local state to the broker's PARTIALLY_FILLED.
    assert machine.current_state(order_id) is OrderState.PARTIALLY_FILLED

    session = SessionMachine(conn=conn)
    session.begin_session()
    state = session.complete_startup(report, resume_target=SessionState.POSITION_MANAGEMENT)
    assert state is SessionState.POSITION_MANAGEMENT
