"""Section 15.2 scheduling model — cadences and priorities, no busy loops.

Two hard properties, both test-enforced:

1. **Hard-risk monitoring cannot be starved.** Open-position risk monitoring
   runs on a tighter interval than new-entry research AND at strictly higher
   priority: whenever both are due, hard-risk runs first. Deterministic
   services are the only things on timed cadences.
2. **LLM agents never run on a tick.** Agent work becomes due ONLY when a
   meaningful state change is queued (:meth:`Scheduler.queue_agent_trigger`)
   or a scheduled analysis window is explicitly opened. A million idle ticks
   produce zero agent invocations.

The scheduler is passive and clockless: callers pass ``now`` in, which makes
every cadence property provable in tests without sleeping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from src.domain.values import DomainValidationError, require_utc

#: Default deterministic cadences (seconds). Hard-risk runs 10x more often
#: than new-entry research, and outranks everything else when due.
HARD_RISK_INTERVAL_SECONDS = 30
RECONCILIATION_INTERVAL_SECONDS = 60
MARKET_DATA_INTERVAL_SECONDS = 60
RESEARCH_SCAN_INTERVAL_SECONDS = 300


@dataclass
class ScheduledTask:
    """One deterministic cadence. Lower priority number = runs first."""

    name: str
    interval_seconds: int
    priority: int
    last_run: datetime | None = None

    def __post_init__(self) -> None:
        if self.interval_seconds < 1:
            raise DomainValidationError("interval_seconds must be >= 1")

    def due(self, now: datetime) -> bool:
        if self.last_run is None:
            return True
        return (now - self.last_run).total_seconds() >= self.interval_seconds


def default_tasks() -> list[ScheduledTask]:
    return [
        ScheduledTask("hard_risk_monitoring", HARD_RISK_INTERVAL_SECONDS, priority=0),
        ScheduledTask("reconciliation", RECONCILIATION_INTERVAL_SECONDS, priority=1),
        ScheduledTask("market_data_refresh", MARKET_DATA_INTERVAL_SECONDS, priority=2),
        ScheduledTask("new_entry_research_scan", RESEARCH_SCAN_INTERVAL_SECONDS, priority=3),
    ]


@dataclass(frozen=True)
class AgentTrigger:
    """A meaningful state change that justifies invoking reasoning agents."""

    reason: str
    queued_at: datetime


@dataclass
class Scheduler:
    """Priority-ordered deterministic cadences plus trigger-gated agent work."""

    tasks: list[ScheduledTask] = field(default_factory=default_tasks)
    _agent_triggers: list[AgentTrigger] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        names = [t.name for t in self.tasks]
        if len(names) != len(set(names)):
            raise DomainValidationError(f"duplicate task names: {names}")
        by_name = {t.name: t for t in self.tasks}
        risk = by_name.get("hard_risk_monitoring")
        research = by_name.get("new_entry_research_scan")
        if risk is not None and research is not None:
            # Structural Section 15.2 invariant, enforced at construction.
            if risk.interval_seconds >= research.interval_seconds:
                raise DomainValidationError(
                    "hard-risk monitoring must run more frequently than new-entry research"
                )
            if risk.priority >= research.priority:
                raise DomainValidationError("hard-risk monitoring must outrank new-entry research")

    # -- deterministic cadences -------------------------------------------

    def due_tasks(self, now: datetime) -> list[ScheduledTask]:
        now = require_utc("now", now)
        return sorted((t for t in self.tasks if t.due(now)), key=lambda t: (t.priority, t.name))

    def run_next(self, now: datetime) -> str | None:
        """Pop and mark the single highest-priority due task (the worst-case
        one-task-per-tick execution model the starvation test exercises)."""
        due = self.due_tasks(now)
        if not due:
            return None
        task = due[0]
        task.last_run = now
        return task.name

    # -- LLM agent gating ---------------------------------------------------

    def queue_agent_trigger(self, reason: str, *, now: datetime) -> None:
        """Record a meaningful state change (regime flip, new catalyst, filled
        order, scheduled analysis window opening, ...)."""
        if not reason:
            raise DomainValidationError("an agent trigger requires a reason")
        self._agent_triggers.append(AgentTrigger(reason, require_utc("now", now)))

    def agent_work_due(self) -> bool:
        return bool(self._agent_triggers)

    def drain_agent_triggers(self) -> tuple[AgentTrigger, ...]:
        """Hand the queued triggers to the agent pipeline exactly once."""
        drained = tuple(self._agent_triggers)
        self._agent_triggers.clear()
        return drained
