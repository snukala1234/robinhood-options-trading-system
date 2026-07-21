"""Strictly read-only FastAPI dashboard (Section 7).

Every route is a GET or a read-only WebSocket. There is NO POST/PUT/PATCH/DELETE and NO
import of the execution / MCP layer — the dashboard cannot place, approve, resize, or cancel
an order. Order placement and approval happen entirely on Robinhood. This module reads the
same SQLite tables the agents write, plus the in-process event bus for live ticks.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from config import settings
from config.guardrails import (
    MAX_CONCURRENT_POSITIONS,
    MAX_DAILY_LOSS_PCT,
    MAX_DRAWDOWN_HALT_PCT,
    MAX_POSITION_PCT_OF_EQUITY,
    MIN_SIGNAL_CONFIDENCE_TO_TRADE,
    ORDER_APPROVAL_MODE,
    PAPER_TRADING,
)
from core.db import Database
from core.event_bus import bus
from core.market_data import get_snapshot

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Equities Trading Firm — Read-Only Dashboard", version="0.1.0")


def get_database() -> Database:
    """Open a fresh read connection (single-user local dashboard)."""
    return Database.connect(settings.DB_PATH)


@app.on_event("startup")
async def _bind_bus_loop() -> None:
    bus.bind_loop(asyncio.get_running_loop())


# --- static SPA -------------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# --- read-only REST ---------------------------------------------------------


@app.get("/api/agents/status")
def agents_status() -> dict[str, Any]:
    db = get_database()
    try:
        return {"agents": db.all_agent_status(), "failovers": db.failover_events(limit=20)}
    finally:
        db.close()


@app.get("/api/guardrails")
def guardrails() -> dict[str, Any]:
    db = get_database()
    try:
        series = db.equity_series()
        latest = series[-1] if series else None
        hwm = db.latest_high_water_mark() or (latest["total_equity"] if latest else 0.0)
        equity = latest["total_equity"] if latest else 0.0
        drawdown = (hwm - equity) / hwm if hwm else 0.0
        pnl_rows = db.daily_pnl_range("2000-01-01", "2100-01-01")
        daily_loss = -min((r["total_pnl_pct"] or 0.0) for r in pnl_rows) if pnl_rows else 0.0
        return {
            "paper_trading": PAPER_TRADING,
            "order_approval_mode": ORDER_APPROVAL_MODE,
            "limits": {
                "max_position_pct_of_equity": MAX_POSITION_PCT_OF_EQUITY,
                "max_concurrent_positions": MAX_CONCURRENT_POSITIONS,
                "max_daily_loss_pct": MAX_DAILY_LOSS_PCT,
                "max_drawdown_halt_pct": MAX_DRAWDOWN_HALT_PCT,
                "min_signal_confidence_to_trade": MIN_SIGNAL_CONFIDENCE_TO_TRADE,
            },
            "current": {
                "open_positions": db.open_position_count(),
                "drawdown": round(drawdown, 4),
                "worst_daily_loss": round(daily_loss, 4),
                "equity": equity,
                "high_water_mark": hwm,
            },
        }
    finally:
        db.close()


_RANGE_DAYS = {"1D": 1, "1W": 7, "1M": 30, "3M": 90}


def _range_start(rng: str) -> str | None:
    now = datetime.now(UTC)
    if rng == "ALL":
        return None
    if rng == "YTD":
        return datetime(now.year, 1, 1, tzinfo=UTC).isoformat()
    return (now - timedelta(days=_RANGE_DAYS.get(rng, 1))).isoformat()


@app.get("/api/equity")
def equity(range: str = "ALL") -> dict[str, Any]:  # noqa: A002 - matches the query param name
    db = get_database()
    try:
        series = db.equity_series(start_ts=_range_start(range))
        return {"range": range, "is_paper": PAPER_TRADING, "points": series}
    finally:
        db.close()


@app.get("/api/calendar")
def calendar(month: str | None = None) -> dict[str, Any]:
    db = get_database()
    try:
        if month is None:
            rows_all = db.daily_pnl_range("2000-01-01", "2100-01-01")
            month = rows_all[-1]["trading_date"][:7] if rows_all else datetime.now(UTC).strftime("%Y-%m")
        year, mon = (int(x) for x in month.split("-"))
        return {"month": month, "days": db.daily_pnl_month(year, mon)}
    finally:
        db.close()


@app.get("/api/day/{day}")
def day_detail(day: str) -> dict[str, Any]:
    db = get_database()
    try:
        return {"date": day, "trades": db.trades_on_date(day)}
    finally:
        db.close()


def _summary_block(db: Database, start: str, end: str, label: str) -> dict[str, Any]:
    rows = db.daily_pnl_range(start, end)
    total = round(sum(r["total_pnl"] or 0.0 for r in rows), 2)
    realized = round(sum(r["realized_pnl"] or 0.0 for r in rows), 2)
    unrealized = round(sum(r["unrealized_pnl_change"] or 0.0 for r in rows), 2)
    trades_closed = sum(r["trades_closed"] or 0 for r in rows)
    wins = sum(r["wins"] or 0 for r in rows)
    losses = sum(r["losses"] or 0 for r in rows)
    closed = [
        t for t in db.closed_trades() if start <= str(t.get("exit_ts") or "")[:10] <= end
    ]
    pnls = [float(t.get("realized_pnl") or 0.0) for t in closed]
    starting = rows[0]["starting_equity"] if rows else 0.0
    return {
        "label": label,
        "total_pnl": total,
        "total_pnl_pct": round(total / starting, 4) if starting else 0.0,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "trades_closed": trades_closed,
        "win_rate": round(wins / (wins + losses), 4) if (wins + losses) else 0.0,
        "best_trade": round(max(pnls), 2) if pnls else 0.0,
        "worst_trade": round(min(pnls), 2) if pnls else 0.0,
        "sparkline": [r["ending_equity"] for r in rows],
    }


@app.get("/api/pnl/summary")
def pnl_summary() -> dict[str, Any]:
    db = get_database()
    try:
        now = datetime.now(UTC).date()
        today = now.isoformat()
        week_start = (now - timedelta(days=now.weekday())).isoformat()
        month_start = now.replace(day=1).isoformat()
        year_start = now.replace(month=1, day=1).isoformat()
        return {
            "is_paper": PAPER_TRADING,
            "today": _summary_block(db, today, today, "Today"),
            "week": _summary_block(db, week_start, today, "This Week"),
            "month": _summary_block(db, month_start, today, "This Month"),
            "year": _summary_block(db, year_start, today, "This Year"),
        }
    finally:
        db.close()


@app.get("/api/positions/open")
def positions_open() -> dict[str, Any]:
    db = get_database()
    try:
        out = []
        for p in db.open_positions():
            price = get_snapshot(p.symbol).current_price
            out.append({
                "symbol": p.symbol,
                "shares": p.shares,
                "entry_price": p.entry_price,
                "current_price": price,
                "market_value": round(p.shares * price, 2),
                "unrealized_pnl": round((price - p.entry_price) * p.shares, 2),
                "stop_loss_pct": p.stop_loss_pct,
                "take_profit_pct": p.take_profit_pct,
            })
        return {"positions": out}
    finally:
        db.close()


@app.get("/api/orders/recent")
def orders_recent() -> dict[str, Any]:
    """DISPLAY ONLY — mirrors what the backend sent to Robinhood. Never originates an order."""
    db = get_database()
    try:
        return {"orders": db.recent_orders(limit=50), "note": "display_only_approval_on_robinhood"}
    finally:
        db.close()


# --- read-only WebSocket streams --------------------------------------------


@app.websocket("/ws/agents")
async def ws_agents(ws: WebSocket) -> None:
    await ws.accept()
    db = get_database()
    try:
        await ws.send_json({"type": "snapshot", "agents": db.all_agent_status()})
    finally:
        db.close()
    queue = bus.async_subscribe()
    try:
        while True:
            event = await queue.get()
            if event.get("type") in ("agent_status", "signal_flow", "failover"):
                await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        bus.async_unsubscribe(queue)


@app.websocket("/ws/equity")
async def ws_equity(ws: WebSocket) -> None:
    await ws.accept()
    queue = bus.async_subscribe()
    try:
        while True:
            event = await queue.get()
            if event.get("type") == "equity":
                await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        bus.async_unsubscribe(queue)
