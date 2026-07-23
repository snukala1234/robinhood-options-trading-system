"""Section 3.2 kill switches on one panel with a monotonic halt epoch.

The halt epoch increments whenever any of the eleven Section 3.2 kill switches
or any Section 3.1 step-7 circuit breaker changes state — activation *and*
manual resume both bump it. The trade gate stamps the current epoch into every
approval token at issuance; the execution adapter re-reads this panel
immediately before broker submit and fails closed if any switch is active or
the epoch has moved. A token can therefore never straddle a halt: any change
in halt state between issuance and submission invalidates it (audit finding 1).

Resuming a cleared switch is a deliberate human action
(``REQUIRE_MANUAL_RESUME_AFTER_HALT``): ``clear`` demands a non-empty
``resumed_by`` identifier and is never called by any automated code path.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from src.config.risk_policy import KILL_SWITCHES, REQUIRE_MANUAL_RESUME_AFTER_HALT
from src.persistence.db import Connection

#: Section 3.1 step-7 circuit breakers. They live on the same panel as the kill
#: switches so tripping one bumps the same halt epoch tokens are bound to.
CIRCUIT_BREAKER_NAMES: tuple[str, ...] = (
    "daily_realized_loss_breach",
    "daily_equity_drawdown_breach",
    "weekly_drawdown_breach",
    "peak_to_trough_drawdown_breach",
)

_KNOWN_SWITCHES = frozenset(KILL_SWITCHES) | frozenset(CIRCUIT_BREAKER_NAMES)

#: Switches that block even risk-reducing exits. Everything else halts new
#: entries while allowing exits (Section 3.2 "new-entry halt" semantics).
EXIT_BLOCKING_SWITCHES = frozenset(
    {"global_trading_halt", "manual_emergency_stop", "broker_degradation"}
)


class UnknownSwitchError(ValueError):
    """A name outside the Section 3.2 / step-7 vocabulary was used."""


class ManualResumeRequired(RuntimeError):
    """Clearing a halt requires an identified human (REQUIRE_MANUAL_RESUME_AFTER_HALT)."""


@dataclass
class KillSwitchPanel:
    """Live halt state shared by the trade gate and the execution adapter."""

    conn: Connection | None = None
    _active: dict[str, str] = field(default_factory=dict, init=False)
    _epoch: int = field(default=0, init=False)

    @property
    def halt_epoch(self) -> int:
        return self._epoch

    def is_active(self, name: str) -> bool:
        self._require_known(name)
        return name in self._active

    def active_switches(self) -> tuple[str, ...]:
        return tuple(sorted(self._active))

    def blocks_new_entries(self) -> tuple[str, ...]:
        """Every active switch blocks new entries (Section 3.2 default: uncertain
        or degraded state means no new entries)."""
        return self.active_switches()

    def blocks_exits(self) -> tuple[str, ...]:
        return tuple(sorted(set(self._active) & EXIT_BLOCKING_SWITCHES))

    def activate(self, name: str, *, reason: str) -> int:
        """Trip a switch. Returns the (possibly bumped) halt epoch."""
        self._require_known(name)
        if not reason or not isinstance(reason, str):
            raise ValueError("a kill switch activation requires a non-empty reason")
        if name in self._active:  # already tripped: no state change, no epoch bump
            return self._epoch
        self._active[name] = reason
        self._epoch += 1
        self._record_event(
            "kill_switch_activated", {"switch": name, "reason": reason, "halt_epoch": self._epoch}
        )
        return self._epoch

    def clear(self, name: str, *, resumed_by: str) -> int:
        """Manually resume from a halt. Human-only; also bumps the epoch, so
        tokens issued under the halted regime die with it."""
        self._require_known(name)
        if REQUIRE_MANUAL_RESUME_AFTER_HALT and (not resumed_by or not isinstance(resumed_by, str)):
            raise ManualResumeRequired(
                "REQUIRE_MANUAL_RESUME_AFTER_HALT: clearing a switch requires an "
                "identified human in resumed_by"
            )
        if name not in self._active:
            return self._epoch
        del self._active[name]
        self._epoch += 1
        self._record_event(
            "kill_switch_cleared",
            {"switch": name, "resumed_by": resumed_by, "halt_epoch": self._epoch},
        )
        return self._epoch

    def _require_known(self, name: str) -> None:
        if name not in _KNOWN_SWITCHES:
            raise UnknownSwitchError(
                f"{name!r} is not a Section 3.2 kill switch or step-7 circuit breaker"
            )

    def _record_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.conn is None:
            return
        self.conn.execute(
            """INSERT INTO system_events
               (id, created_at, severity, component, event_type, correlation_id, payload)
               VALUES (%s, %s, 'critical', 'kill_switches', %s, NULL, %s)""",
            (uuid.uuid4(), datetime.now(UTC), event_type, Jsonb(payload)),
        )
