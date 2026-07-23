"""Section 15.2 scheduling (hard-risk never starved, agents never on a tick)
and the event bus (failure-isolating, journaled)."""

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from src.domain.values import DomainValidationError
from src.orchestration.events import Event, EventBus
from src.orchestration.scheduler import ScheduledTask, Scheduler

T0 = datetime(2026, 7, 22, 13, 30, tzinfo=UTC)


def test_hard_risk_cadence_is_structurally_tighter_than_research() -> None:
    scheduler = Scheduler()
    by_name = {t.name: t for t in scheduler.tasks}
    risk = by_name["hard_risk_monitoring"]
    research = by_name["new_entry_research_scan"]
    assert risk.interval_seconds < research.interval_seconds
    assert risk.priority < research.priority


def test_inverted_cadence_is_unconstructible() -> None:
    with pytest.raises(DomainValidationError, match="more frequently"):
        Scheduler(
            tasks=[
                ScheduledTask("hard_risk_monitoring", 300, priority=0),
                ScheduledTask("new_entry_research_scan", 30, priority=3),
            ]
        )
    with pytest.raises(DomainValidationError, match="outrank"):
        Scheduler(
            tasks=[
                ScheduledTask("hard_risk_monitoring", 30, priority=5),
                ScheduledTask("new_entry_research_scan", 300, priority=3),
            ]
        )


def test_hard_risk_wins_whenever_both_are_due() -> None:
    scheduler = Scheduler()
    due = scheduler.due_tasks(T0)  # nothing has ever run: everything is due
    assert due[0].name == "hard_risk_monitoring"


def test_hard_risk_monitoring_is_not_starved_by_research() -> None:
    """Worst case: one task per 10s tick for a simulated hour. Hard-risk must
    keep its cadence (gaps bounded) and outrun the research scan."""
    scheduler = Scheduler()
    runs: dict[str, list[datetime]] = {}
    for tick in range(360):  # 3600s at 10s ticks
        now = T0 + timedelta(seconds=10 * tick)
        name = scheduler.run_next(now)
        if name is not None:
            runs.setdefault(name, []).append(now)

    risk_runs = runs["hard_risk_monitoring"]
    research_runs = runs.get("new_entry_research_scan", [])
    assert len(research_runs) >= 1  # research still runs...
    assert len(risk_runs) > len(research_runs)  # ...but never ahead of risk
    assert len(risk_runs) >= 80  # ~120 ideal at 30s; contention costs a little
    gaps = [(b - a).total_seconds() for a, b in itertools.pairwise(risk_runs)]
    assert max(gaps) <= 60  # never more than 2x the 30s cadence


def test_agents_are_never_invoked_on_a_tick() -> None:
    scheduler = Scheduler()
    for tick in range(1000):
        scheduler.run_next(T0 + timedelta(seconds=10 * tick))
        assert not scheduler.agent_work_due()
    assert scheduler.drain_agent_triggers() == ()


def test_agents_run_only_on_meaningful_triggers() -> None:
    scheduler = Scheduler()
    scheduler.queue_agent_trigger("regime_change_detected", now=T0)
    scheduler.queue_agent_trigger("premarket_analysis_window", now=T0)
    assert scheduler.agent_work_due()
    drained = scheduler.drain_agent_triggers()
    assert [t.reason for t in drained] == [
        "regime_change_detected",
        "premarket_analysis_window",
    ]
    # Drained exactly once: the queue is now empty.
    assert not scheduler.agent_work_due()
    assert scheduler.drain_agent_triggers() == ()
    with pytest.raises(DomainValidationError):
        scheduler.queue_agent_trigger("", now=T0)


# --- event bus ----------------------------------------------------------------


def test_bus_dispatches_to_subscribers() -> None:
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe("order_filled", lambda e: seen.append(str(e.payload["order_id"])))
    count = bus.publish(Event("order_filled", {"order_id": "o-1"}))
    assert count == 1 and seen == ["o-1"]


def test_bus_isolates_handler_failures(conn: psycopg.Connection[Any]) -> None:
    bus = EventBus(conn=conn)
    seen: list[str] = []

    def exploding(_: Event) -> None:
        raise RuntimeError("handler bug")

    bus.subscribe("kill_switch_tripped", exploding)
    bus.subscribe("kill_switch_tripped", lambda e: seen.append(e.event_type))
    correlation = uuid.uuid4()
    count = bus.publish(Event("kill_switch_tripped", {"switch": "drawdown_breach"}, correlation))
    assert count == 1  # the second handler still ran
    assert seen == ["kill_switch_tripped"]

    rows = conn.execute(
        "SELECT * FROM system_events WHERE component = 'event_bus' ORDER BY created_at"
    ).fetchall()
    assert [r["event_type"] for r in rows] == ["kill_switch_tripped", "event_handler_failed"]
    assert rows[1]["severity"] == "critical"
    assert rows[1]["payload"]["source_event"] == "kill_switch_tripped"
    assert rows[0]["correlation_id"] == correlation


def test_bus_journals_events_with_no_subscribers(conn: psycopg.Connection[Any]) -> None:
    bus = EventBus(conn=conn)
    assert bus.publish(Event("session_transition", {"to": "DEGRADED"})) == 0
    row = conn.execute("SELECT * FROM system_events WHERE component = 'event_bus'").fetchone()
    assert row is not None and row["event_type"] == "session_transition"


def test_event_requires_a_type() -> None:
    with pytest.raises(ValueError):
        Event("", {})
