"""The read-only dashboard app: GET routes and a server-push WebSocket.

Every HTTP route is GET over a SELECT-only connection. The WebSocket accepts
exactly two inbound frame types — ``{"type": "ping"}`` and
``{"type": "subscribe", "panel": <name>}`` — as connection lifecycle; every
other inbound frame (commands, oversized payloads, malformed JSON) is
counted, logged, and dropped. There is no code path from an inbound frame to
anything that touches trading state, because nothing in this package can
reach trading state at all.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from psycopg import Connection
from psycopg.rows import DictRow

from src.config.environments import ConfigurationError
from src.dashboard.panels import PANELS, _banner

logger = logging.getLogger("dashboard.ws")

#: Inbound WS frames longer than this are ignored outright (never parsed).
MAX_WS_FRAME_BYTES = 4096

ConnectionProvider = Callable[[], AbstractContextManager[Connection[DictRow]]]

_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def create_app(connection_provider: ConnectionProvider) -> FastAPI:
    """Build the read-only app over a (SELECT-only) connection provider."""
    app = FastAPI(
        title="Options V2 dashboard (read-only)",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.ignored_ws_frames = 0

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "banner": _banner(),
            "ignored_ws_frames": app.state.ignored_ws_frames,
        }

    @app.get("/api/panels/{panel_name}")
    def panel(panel_name: str) -> dict[str, object]:
        fn = PANELS.get(panel_name)
        if fn is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="unknown panel")
        with connection_provider() as conn:
            return fn(conn)

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_json({"type": "hello", "banner": _banner()})
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                raw = message.get("text")
                if raw is None:
                    payload = message.get("bytes") or b""
                    if len(payload) > MAX_WS_FRAME_BYTES:
                        _ignore(app, "oversized binary frame", repr(payload[:40]))
                        continue
                    try:
                        raw = payload.decode("utf-8")
                    except UnicodeDecodeError:
                        _ignore(app, "binary frame", repr(payload[:40]))
                        continue
                if len(raw.encode("utf-8", errors="replace")) > MAX_WS_FRAME_BYTES:
                    _ignore(app, "oversized frame", raw[:80])
                    continue
                try:
                    frame = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    _ignore(app, "malformed JSON", raw[:200])
                    continue
                if not isinstance(frame, dict):
                    _ignore(app, "non-object frame", raw[:200])
                    continue
                frame_type = frame.get("type")
                if frame_type == "ping":
                    await websocket.send_json({"type": "pong"})
                elif frame_type == "subscribe":
                    panel_name = str(frame.get("panel", ""))
                    fn = PANELS.get(panel_name)
                    if fn is None:
                        _ignore(app, "subscribe to unknown panel", panel_name[:80])
                        continue
                    with connection_provider() as conn:
                        await websocket.send_json(
                            {"type": "panel", "panel": panel_name, "data": fn(conn)}
                        )
                else:
                    # Anything else — including anything shaped like a command
                    # ("place an order") — is dropped here. It is dispatched to
                    # nothing; no handler beyond this counter ever sees it.
                    _ignore(app, "unsupported frame type", raw[:200])
        except WebSocketDisconnect:
            return

    return app


def _ignore(app: FastAPI, reason: str, sample: str) -> None:
    app.state.ignored_ws_frames += 1
    logger.warning(
        "ignored inbound websocket frame (%s): %r — the dashboard socket is "
        "server-push only and dispatches inbound frames to nothing",
        reason,
        sample,
    )


def serve(app: FastAPI, *, host: str = "127.0.0.1", port: int = 8404) -> None:
    """Run the dashboard, localhost-bound only (Section 17)."""
    if host not in _LOCAL_HOSTS:
        raise ConfigurationError(
            f"dashboard host {host!r} refused: bind to localhost only (Section 17)"
        )
    import uvicorn

    uvicorn.run(app, host=host, port=port)
