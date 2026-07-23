"""Session state machine: the exact Section 15 chain, universal DEGRADED/HALTED
entry, and explicitly restricted recovery edges."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from src.orchestration.health import CheckResult
from src.orchestration.session import (
    SessionMachine,
    SessionState,
    SessionTransitionError,
)
from src.orchestration.startup import StartupValidationReport

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)

DAY_CHAIN = (
    SessionState.MARKET_OPEN_OBSERVATION,
    SessionState.ENTRY_WINDOW,
    SessionState.POSITION_MANAGEMENT,
    SessionState.ENTRY_PAUSED,
    SessionState.MARKET_CLOSE_RECONCILIATION,
    SessionState.POSTMARKET_AUDIT,
    SessionState.OFFLINE,
)

NON_SPECIAL_STATES = tuple(
    s for s in SessionState if s not in (SessionState.DEGRADED, SessionState.HALTED)
)


def _report(passed: bool) -> StartupValidationReport:
    check = CheckResult("probe", passed, "test")
    return StartupValidationReport(
        started_at=NOW,
        checks=(check,),
        mode_banner="MODE: PAPER",
        broker_positions=0,
        open_orders=0,
    )


def test_full_day_walk_matches_the_spec_chain(conn: psycopg.Connection[Any]) -> None:
    machine = SessionMachine(conn=conn)
    assert machine.state is SessionState.OFFLINE
    machine.begin_session()
    machine.complete_startup(_report(passed=True))
    assert machine.state is SessionState.PREMARKET_RESEARCH
    for state in DAY_CHAIN:
        machine.transition(state)
    assert machine.state is SessionState.OFFLINE
    rows = conn.execute(
        "SELECT payload FROM system_events WHERE component = 'session' ORDER BY created_at"
    ).fetchall()
    walked = [(r["payload"]["previous_state"], r["payload"]["new_state"]) for r in rows]
    assert walked[0] == ("OFFLINE", "STARTUP_VALIDATION")
    assert walked[1] == ("STARTUP_VALIDATION", "PREMARKET_RESEARCH")
    assert walked[-1] == ("POSTMARKET_AUDIT", "OFFLINE")
    assert len(walked) == 9


def test_illegal_transitions_are_refused() -> None:
    machine = SessionMachine()
    with pytest.raises(SessionTransitionError):
        machine.transition(SessionState.ENTRY_WINDOW)  # OFFLINE cannot jump in
    machine = SessionMachine(initial=SessionState.PREMARKET_RESEARCH)
    with pytest.raises(SessionTransitionError):
        machine.transition(SessionState.POSTMARKET_AUDIT)


def test_degraded_and_halted_require_their_dedicated_methods() -> None:
    machine = SessionMachine(initial=SessionState.ENTRY_WINDOW)
    with pytest.raises(SessionTransitionError, match="enter_degraded"):
        machine.transition(SessionState.DEGRADED)
    with pytest.raises(SessionTransitionError, match="halt"):
        machine.transition(SessionState.HALTED)


@pytest.mark.parametrize("state", NON_SPECIAL_STATES)
def test_degraded_is_enterable_from_any_state(state: SessionState) -> None:
    machine = SessionMachine(initial=state)
    assert machine.enter_degraded(["data feed lost"]) is SessionState.DEGRADED
    assert machine.degradation_reasons == ("data feed lost",)


@pytest.mark.parametrize("state", [*NON_SPECIAL_STATES, SessionState.DEGRADED])
def test_halted_is_enterable_from_any_state_including_degraded(state: SessionState) -> None:
    machine = SessionMachine(initial=state)
    assert machine.halt("manual emergency stop") is SessionState.HALTED


def test_degraded_reasons_accumulate_idempotently() -> None:
    machine = SessionMachine(initial=SessionState.ENTRY_WINDOW)
    machine.enter_degraded(["broker degraded"])
    machine.enter_degraded(["broker degraded", "stale quotes"])
    assert machine.state is SessionState.DEGRADED
    assert machine.degradation_reasons == ("broker degraded", "stale quotes")


def test_halted_cannot_self_exit() -> None:
    machine = SessionMachine(initial=SessionState.POSITION_MANAGEMENT)
    machine.halt("drawdown breach")
    with pytest.raises(SessionTransitionError, match="no automatic exit"):
        machine.transition(SessionState.POSITION_MANAGEMENT)
    with pytest.raises(SessionTransitionError, match="only applies in DEGRADED"):
        machine.resume_from_degraded(SessionState.ENTRY_PAUSED, current_conditions=())
    with pytest.raises(SessionTransitionError, match="identified human"):
        machine.manual_resume(SessionState.ENTRY_PAUSED, resumed_by="")
    assert machine.state is SessionState.HALTED  # nothing above moved it


def test_manual_resume_requires_safe_target() -> None:
    machine = SessionMachine(initial=SessionState.ENTRY_WINDOW)
    machine.halt("test")
    with pytest.raises(SessionTransitionError, match="not a safe resume target"):
        machine.manual_resume(SessionState.ENTRY_WINDOW, resumed_by="operator")
    assert (
        machine.manual_resume(SessionState.ENTRY_PAUSED, resumed_by="operator")
        is SessionState.ENTRY_PAUSED
    )


def test_degraded_resumes_only_when_condition_cleared() -> None:
    machine = SessionMachine(initial=SessionState.ENTRY_WINDOW)
    machine.enter_degraded(["reconciliation mismatch"])
    with pytest.raises(SessionTransitionError, match="not cleared"):
        machine.resume_from_degraded(
            SessionState.POSITION_MANAGEMENT,
            current_conditions=("1 order(s) in RECONCILIATION_REQUIRED",),
        )
    with pytest.raises(SessionTransitionError, match="only via resume_from_degraded"):
        machine.transition(SessionState.ENTRY_WINDOW)
    with pytest.raises(SessionTransitionError, match="not a safe resume target"):
        machine.resume_from_degraded(SessionState.ENTRY_WINDOW, current_conditions=())
    resumed = machine.resume_from_degraded(SessionState.POSITION_MANAGEMENT, current_conditions=())
    assert resumed is SessionState.POSITION_MANAGEMENT
    assert machine.degradation_reasons == ()


def test_halted_outranks_degraded() -> None:
    machine = SessionMachine(initial=SessionState.ENTRY_WINDOW)
    machine.halt("stop")
    with pytest.raises(SessionTransitionError, match="outranks"):
        machine.enter_degraded(["anything"])


def test_startup_validation_fails_closed() -> None:
    machine = SessionMachine()
    machine.begin_session()
    # The forward edge is not reachable through plain transition().
    with pytest.raises(SessionTransitionError, match="complete_startup"):
        machine.transition(SessionState.PREMARKET_RESEARCH)
    # A failed report refuses to advance and the session stays put.
    with pytest.raises(SessionTransitionError, match="remains in STARTUP_VALIDATION"):
        machine.complete_startup(_report(passed=False))
    assert machine.state is SessionState.STARTUP_VALIDATION
    # Aborting back to OFFLINE is always allowed.
    machine.transition(SessionState.OFFLINE)
    assert machine.state is SessionState.OFFLINE


def test_startup_resume_targets_are_restricted() -> None:
    machine = SessionMachine()
    machine.begin_session()
    with pytest.raises(SessionTransitionError, match="not a legal post-validation target"):
        machine.complete_startup(_report(passed=True), resume_target=SessionState.ENTRY_WINDOW)
    state = machine.complete_startup(
        _report(passed=True), resume_target=SessionState.POSITION_MANAGEMENT
    )
    assert state is SessionState.POSITION_MANAGEMENT


def test_complete_startup_only_applies_in_startup_validation() -> None:
    machine = SessionMachine(initial=SessionState.ENTRY_WINDOW)
    with pytest.raises(SessionTransitionError, match="only applies in STARTUP_VALIDATION"):
        machine.complete_startup(_report(passed=True))


def test_degraded_and_halt_require_reasons() -> None:
    machine = SessionMachine(initial=SessionState.ENTRY_WINDOW)
    with pytest.raises(SessionTransitionError):
        machine.enter_degraded([])
    with pytest.raises(SessionTransitionError):
        machine.halt("")
