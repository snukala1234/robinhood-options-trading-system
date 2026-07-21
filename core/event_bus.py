"""Lightweight in-process event bus (Section 7.1).

Agents publish a status/signal-flow/equity event on every state change. The
dashboard's WebSocket layer relays these to the browser. The bus keeps a bounded
history so a freshly-connected client can be sent the current state immediately,
and supports both synchronous subscribers (in-process consumers) and asyncio
queue subscribers (the WebSocket relay running on the FastAPI event loop).

Publishing is deliberately best-effort and never raises into the caller: a
telemetry/event failure must never break a trading decision path.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from typing import Any

_log = logging.getLogger("event_bus")

Event = dict[str, Any]


class EventBus:
    """Synchronous fan-out with an optional asyncio bridge for WebSocket relay."""

    def __init__(self, history_size: int = 500) -> None:
        self._subscribers: list[Callable[[Event], None]] = []
        self._history: deque[Event] = deque(maxlen=history_size)
        self._async_queues: set[asyncio.Queue[Event]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the asyncio loop used to deliver events to async queue subscribers."""
        self._loop = loop

    # -- sync subscribers --------------------------------------------------

    def subscribe(self, callback: Callable[[Event], None]) -> Callable[[Event], None]:
        self._subscribers.append(callback)
        return callback

    def unsubscribe(self, callback: Callable[[Event], None]) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    # -- async subscribers (WebSocket relay) -------------------------------

    def async_subscribe(self, maxsize: int = 1000) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._async_queues.add(queue)
        return queue

    def async_unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._async_queues.discard(queue)

    # -- publish -----------------------------------------------------------

    def publish(self, event: Event) -> None:
        self._history.append(event)
        for cb in list(self._subscribers):
            try:
                cb(event)
            except Exception:  # noqa: BLE001 - telemetry must never break callers
                _log.exception("event subscriber raised; continuing")
        loop = self._loop
        if loop is not None and self._async_queues:
            for queue in list(self._async_queues):
                loop.call_soon_threadsafe(self._safe_put, queue, event)

    @staticmethod
    def _safe_put(queue: asyncio.Queue[Event], event: Event) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            _log.warning("async event queue full; dropping event")

    def history(self, limit: int | None = None) -> list[Event]:
        items = list(self._history)
        if limit is not None:
            items = items[-limit:]
        return items


# Process-wide singleton shared by the agents and the dashboard.
bus = EventBus()


def publish_agent_status(
    agent_name: str,
    state: str,
    summary: str | None = None,
    active_model: str | None = None,
    ts: str | None = None,
) -> Event:
    """Publish (and return) a standard agent-status event."""
    from core.util import utcnow_iso

    event: Event = {
        "type": "agent_status",
        "agent_name": agent_name,
        "state": state,
        "summary": summary,
        "active_model": active_model,
        "ts": ts or utcnow_iso(),
    }
    bus.publish(event)
    return event


def publish_signal_flow(stage: str, symbol: str, detail: str, ts: str | None = None) -> Event:
    """Publish a signal-flow pipeline event (Scanner -> ... -> Execution)."""
    from core.util import utcnow_iso

    event: Event = {
        "type": "signal_flow",
        "stage": stage,
        "symbol": symbol,
        "detail": detail,
        "ts": ts or utcnow_iso(),
    }
    bus.publish(event)
    return event


def publish_equity_tick(snapshot: dict[str, Any]) -> Event:
    """Publish a live equity-snapshot append for the balance chart."""
    event: Event = {"type": "equity", **snapshot}
    bus.publish(event)
    return event
