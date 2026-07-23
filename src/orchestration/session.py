"""The Section 15 session state machine.

The normal day is the exact spec chain::

    OFFLINE -> STARTUP_VALIDATION -> PREMARKET_RESEARCH
    -> MARKET_OPEN_OBSERVATION -> ENTRY_WINDOW <-> POSITION_MANAGEMENT
    -> ENTRY_PAUSED -> MARKET_CLOSE_RECONCILIATION -> POSTMARKET_AUDIT
    -> OFFLINE

plus ``any state -> DEGRADED`` and ``any state -> HALTED``. The recovery
edges are as explicit as the entry edges:

- **STARTUP_VALIDATION** can only be left through :meth:`complete_startup`
  with a *passed* validation report — a failed check keeps the session there
  (fail closed). Aborting back to OFFLINE is always allowed.
- **DEGRADED** resumes via :meth:`resume_from_degraded` ONLY when the caller
  proves the degrading condition cleared (an empty current-conditions list),
  and only into a safe target — a state that cannot immediately open new
  entries. ``transition()`` refuses to leave DEGRADED.
- **HALTED** has no automatic exit of any kind
  (``REQUIRE_MANUAL_RESUME_AFTER_HALT=True``): the only edge out is
  :meth:`manual_resume` with a non-empty human identifier, into a safe
  target. ``transition()`` and ``resume_from_degraded`` both refuse.

Every change is journaled to ``system_events`` (component ``session``).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from src.config.risk_policy import REQUIRE_MANUAL_RESUME_AFTER_HALT
from src.persistence.db import Connection

if TYPE_CHECKING:
    from src.orchestration.startup import StartupValidationReport


class SessionState(StrEnum):
    OFFLINE = "OFFLINE"
    STARTUP_VALIDATION = "STARTUP_VALIDATION"
    PREMARKET_RESEARCH = "PREMARKET_RESEARCH"
    MARKET_OPEN_OBSERVATION = "MARKET_OPEN_OBSERVATION"
    ENTRY_WINDOW = "ENTRY_WINDOW"
    POSITION_MANAGEMENT = "POSITION_MANAGEMENT"
    ENTRY_PAUSED = "ENTRY_PAUSED"
    MARKET_CLOSE_RECONCILIATION = "MARKET_CLOSE_RECONCILIATION"
    POSTMARKET_AUDIT = "POSTMARKET_AUDIT"
    DEGRADED = "DEGRADED"
    HALTED = "HALTED"


#: Normal-operation edges. DEGRADED/HALTED entry edges are universal and
#: handled separately; exits from them exist ONLY via the dedicated methods.
_NORMAL_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.OFFLINE: frozenset({SessionState.STARTUP_VALIDATION}),
    # PREMARKET_RESEARCH is reachable only through complete_startup().
    SessionState.STARTUP_VALIDATION: frozenset({SessionState.OFFLINE}),
    SessionState.PREMARKET_RESEARCH: frozenset({SessionState.MARKET_OPEN_OBSERVATION}),
    SessionState.MARKET_OPEN_OBSERVATION: frozenset({SessionState.ENTRY_WINDOW}),
    SessionState.ENTRY_WINDOW: frozenset(
        {SessionState.POSITION_MANAGEMENT, SessionState.ENTRY_PAUSED}
    ),
    SessionState.POSITION_MANAGEMENT: frozenset(
        {
            SessionState.ENTRY_WINDOW,
            SessionState.ENTRY_PAUSED,
            SessionState.MARKET_CLOSE_RECONCILIATION,
        }
    ),
    SessionState.ENTRY_PAUSED: frozenset(
        {
            SessionState.ENTRY_WINDOW,
            SessionState.POSITION_MANAGEMENT,
            SessionState.MARKET_CLOSE_RECONCILIATION,
        }
    ),
    SessionState.MARKET_CLOSE_RECONCILIATION: frozenset({SessionState.POSTMARKET_AUDIT}),
    SessionState.POSTMARKET_AUDIT: frozenset({SessionState.OFFLINE}),
    SessionState.DEGRADED: frozenset(),
    SessionState.HALTED: frozenset(),
}

#: Recovery may only land in a state that cannot immediately open new entries.
SAFE_RESUME_TARGETS = frozenset(
    {
        SessionState.OFFLINE,
        SessionState.ENTRY_PAUSED,
        SessionState.POSITION_MANAGEMENT,
        SessionState.MARKET_CLOSE_RECONCILIATION,
    }
)

#: Targets complete_startup() may resume into. PREMARKET_RESEARCH is the
#: normal morning path; POSITION_MANAGEMENT/ENTRY_PAUSED cover a mid-session
#: restart with open positions to manage.
STARTUP_RESUME_TARGETS = frozenset(
    {
        SessionState.PREMARKET_RESEARCH,
        SessionState.POSITION_MANAGEMENT,
        SessionState.ENTRY_PAUSED,
    }
)


class SessionTransitionError(RuntimeError):
    """An illegal session transition was refused."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class SessionMachine:
    """Current session state plus the only legal ways to change it."""

    conn: Connection | None = None
    clock: Callable[[], datetime] = _utcnow
    initial: SessionState = SessionState.OFFLINE
    _state: SessionState = field(init=False)
    _degradation_reasons: tuple[str, ...] = field(default=(), init=False)
    _halt_reason: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._state = self.initial

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def degradation_reasons(self) -> tuple[str, ...]:
        return self._degradation_reasons

    # -- normal-operation edges -------------------------------------------

    def transition(self, new: SessionState, *, reason: str = "") -> SessionState:
        """Apply a normal-operation edge. Everything special is refused here:
        entering/leaving DEGRADED or HALTED and leaving STARTUP_VALIDATION
        forward all have dedicated methods with their own preconditions."""
        if new in (SessionState.DEGRADED, SessionState.HALTED):
            raise SessionTransitionError(
                f"use enter_degraded()/halt() to enter {new.value}, with a reason"
            )
        if self._state is SessionState.HALTED:
            raise SessionTransitionError(
                "HALTED has no automatic exit; a manual identified-human resume "
                "is required (REQUIRE_MANUAL_RESUME_AFTER_HALT)"
            )
        if self._state is SessionState.DEGRADED:
            raise SessionTransitionError(
                "DEGRADED is left only via resume_from_degraded() once the "
                "degrading condition has cleared"
            )
        if self._state is SessionState.STARTUP_VALIDATION and new is not SessionState.OFFLINE:
            raise SessionTransitionError(
                "STARTUP_VALIDATION is left forward only via complete_startup() "
                "with a passed validation report"
            )
        if new not in _NORMAL_TRANSITIONS[self._state]:
            raise SessionTransitionError(
                f"illegal session transition {self._state.value} -> {new.value}"
            )
        return self._apply(new, reason or "normal transition")

    def begin_session(self) -> SessionState:
        return self.transition(SessionState.STARTUP_VALIDATION, reason="session start")

    def complete_startup(
        self,
        report: StartupValidationReport,
        *,
        resume_target: SessionState = SessionState.PREMARKET_RESEARCH,
    ) -> SessionState:
        """Leave STARTUP_VALIDATION forward — only with a fully passed report.

        A failed report raises and the session STAYS in STARTUP_VALIDATION:
        a failed check blocks the session (spec 15.1)."""
        if self._state is not SessionState.STARTUP_VALIDATION:
            raise SessionTransitionError(
                f"complete_startup() only applies in STARTUP_VALIDATION, not {self._state.value}"
            )
        if resume_target not in STARTUP_RESUME_TARGETS:
            raise SessionTransitionError(
                f"{resume_target.value} is not a legal post-validation target"
            )
        if not report.passed:
            raise SessionTransitionError(
                "startup validation failed; session remains in STARTUP_VALIDATION: "
                + "; ".join(report.blocking_reasons)
            )
        return self._apply(
            resume_target,
            f"startup validation passed ({len(report.checks)} checks); {report.mode_banner}",
        )

    # -- degradation and halt (universal entry edges) ----------------------

    def enter_degraded(self, reasons: Sequence[str]) -> SessionState:
        """Any state -> DEGRADED (idempotent; reasons accumulate)."""
        if not reasons:
            raise SessionTransitionError("entering DEGRADED requires at least one reason")
        if self._state is SessionState.HALTED:
            raise SessionTransitionError("HALTED outranks DEGRADED; resume manually first")
        merged = tuple(dict.fromkeys((*self._degradation_reasons, *reasons)))
        self._degradation_reasons = merged
        if self._state is SessionState.DEGRADED:
            return self._state
        return self._apply(SessionState.DEGRADED, "; ".join(reasons))

    def halt(self, reason: str) -> SessionState:
        """Any state -> HALTED (idempotent). The strongest state: only an
        identified human ever leaves it."""
        if not reason:
            raise SessionTransitionError("halting requires a reason")
        self._halt_reason = reason
        if self._state is SessionState.HALTED:
            return self._state
        return self._apply(SessionState.HALTED, reason)

    # -- explicitly restricted recovery edges ------------------------------

    def resume_from_degraded(
        self, target: SessionState, *, current_conditions: Sequence[str]
    ) -> SessionState:
        """DEGRADED -> a safe target, ONLY once the degrading condition has
        cleared. ``current_conditions`` is the caller's live re-check (e.g.
        ``degraded_mode_status(...).entry_block_reasons``); anything still
        present refuses the resume."""
        if self._state is not SessionState.DEGRADED:
            raise SessionTransitionError(
                f"resume_from_degraded() only applies in DEGRADED, not {self._state.value}"
            )
        if current_conditions:
            raise SessionTransitionError(
                "degrading condition has not cleared: " + "; ".join(current_conditions)
            )
        if target not in SAFE_RESUME_TARGETS:
            raise SessionTransitionError(
                f"{target.value} is not a safe resume target; allowed: "
                + ", ".join(sorted(s.value for s in SAFE_RESUME_TARGETS))
            )
        self._degradation_reasons = ()
        return self._apply(target, "degrading condition cleared")

    def manual_resume(self, target: SessionState, *, resumed_by: str) -> SessionState:
        """HALTED -> a safe target. The ONLY exit from HALTED, and it demands
        an identified human (REQUIRE_MANUAL_RESUME_AFTER_HALT=True)."""
        if self._state is not SessionState.HALTED:
            raise SessionTransitionError(
                f"manual_resume() only applies in HALTED, not {self._state.value}"
            )
        if REQUIRE_MANUAL_RESUME_AFTER_HALT and (not resumed_by or not isinstance(resumed_by, str)):
            raise SessionTransitionError(
                "REQUIRE_MANUAL_RESUME_AFTER_HALT: resuming from HALTED requires "
                "an identified human in resumed_by"
            )
        if target not in SAFE_RESUME_TARGETS:
            raise SessionTransitionError(
                f"{target.value} is not a safe resume target; allowed: "
                + ", ".join(sorted(s.value for s in SAFE_RESUME_TARGETS))
            )
        self._halt_reason = None
        self._degradation_reasons = ()
        return self._apply(target, f"manual resume by {resumed_by}")

    # -- internal ----------------------------------------------------------

    def _apply(self, new: SessionState, reason: str) -> SessionState:
        previous = self._state
        self._state = new
        self._journal(
            {
                "previous_state": previous.value,
                "new_state": new.value,
                "reason": reason,
            }
        )
        return new

    def _journal(self, payload: dict[str, Any]) -> None:
        if self.conn is None:
            return
        self.conn.execute(
            """INSERT INTO system_events
               (id, created_at, severity, component, event_type, correlation_id, payload)
               VALUES (%s, %s, %s, 'session', 'session_transition', NULL, %s)""",
            (
                uuid.uuid4(),
                self.clock(),
                "critical" if payload["new_state"] in ("DEGRADED", "HALTED") else "info",
                Jsonb(payload),
            ),
        )
