"""In-process event bus: synchronous, deterministic, failure-isolating.

Components publish typed events; subscribers react. One misbehaving handler
never silences the others — its exception is caught, recorded as a critical
system event, and dispatch continues. Every published event can be journaled
to ``system_events`` for the audit trail.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from src.persistence.db import Connection


@dataclass(frozen=True)
class Event:
    event_type: str
    payload: dict[str, Any]
    correlation_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if not self.event_type or not isinstance(self.event_type, str):
            raise ValueError("event_type must be a non-empty string")


Handler = Callable[[Event], None]


@dataclass
class EventBus:
    conn: Connection | None = None
    journal_events: bool = True
    _subscribers: dict[str, list[Handler]] = field(default_factory=dict, init=False)

    def subscribe(self, event_type: str, handler: Handler) -> None:
        self._subscribers.setdefault(event_type, []).append(handler)

    def publish(self, event: Event) -> int:
        """Dispatch to every subscriber; returns how many handlers succeeded."""
        if self.conn is not None and self.journal_events:
            self._record(
                severity="info",
                event_type=event.event_type,
                correlation_id=event.correlation_id,
                payload=event.payload,
            )
        succeeded = 0
        for handler in self._subscribers.get(event.event_type, []):
            try:
                handler(event)
                succeeded += 1
            except Exception as exc:  # noqa: BLE001 - isolate handler failures
                self._record(
                    severity="critical",
                    event_type="event_handler_failed",
                    correlation_id=event.correlation_id,
                    payload={
                        "source_event": event.event_type,
                        "handler": getattr(handler, "__qualname__", repr(handler)),
                        "error": str(exc),
                    },
                )
        return succeeded

    def _record(
        self,
        *,
        severity: str,
        event_type: str,
        correlation_id: uuid.UUID | None,
        payload: dict[str, Any],
    ) -> None:
        if self.conn is None:
            return
        self.conn.execute(
            """INSERT INTO system_events
               (id, created_at, severity, component, event_type, correlation_id, payload)
               VALUES (%s, %s, %s, 'event_bus', %s, %s, %s)""",
            (uuid.uuid4(), datetime.now(UTC), severity, event_type, correlation_id, Jsonb(payload)),
        )
