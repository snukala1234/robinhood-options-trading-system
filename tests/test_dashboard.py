"""Dashboard tests — read-only by construction, serves real backend data."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config import settings
from core.db import Database


def seed(path: Path) -> None:
    db = Database.connect(path)
    cid = db.insert_config_version({"strategy": {}}, promoted_by="human_confirmed", is_active=True)
    db.set_active_config(cid)
    tid = db.insert_trade_entry(
        symbol="AMZN",
        entry_price=100.0,
        position_size_usd=50.0,
        shares=0.5,
        contributing_agents={"research_technical": {"raw_signal": "long", "confidence": 0.8}},
        aggregated_confidence=0.78,
        account_equity_at_entry=300.0,
        atr_pct_at_entry=0.02,
        market_regime_at_entry="normal",
        stop_loss_pct=0.18,
        take_profit_pct=0.25,
        config_version_id=cid,
        active_model="claude-fable-5",
        entry_ts="2026-06-01T14:00:00+00:00",
    )
    db.close_trade(
        tid,
        exit_price=125.0,
        exit_reason="take_profit",
        realized_pnl=12.5,
        holding_period_hours=48.0,
        exit_ts="2026-06-03T14:00:00+00:00",
    )
    db.insert_equity_snapshot(
        total_equity=312.5,
        settled_cash=262.5,
        open_positions_value=0.0,
        high_water_mark=312.5,
        open_position_count=0,
        source="scheduled_close",
        ts="2026-06-03T20:00:00+00:00",
    )
    db.upsert_daily_pnl(
        trading_date="2026-06-03",
        starting_equity=300.0,
        ending_equity=312.5,
        realized_pnl=12.5,
        unrealized_pnl_change=0.0,
        total_pnl=12.5,
        total_pnl_pct=0.0417,
        trades_opened=0,
        trades_closed=1,
        wins=1,
        losses=0,
    )
    db.insert_order(
        symbol="AMZN",
        side="buy",
        quantity=0.5,
        notional_usd=50.0,
        estimated_price=100.0,
        status="filled",
        approval_mode="manual",
    )
    db.upsert_agent_status(
        agent_name="scanner", state="idle", summary="done", active_model="claude-fable-5"
    )
    db.close()


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    dbp = tmp_path / "dash.db"
    seed(dbp)
    monkeypatch.setattr(settings, "DB_PATH", dbp)
    from dashboard.api import app

    return TestClient(app)


# === read-only by construction =============================================


def test_no_state_mutating_routes() -> None:
    from dashboard.api import app

    for route in app.routes:
        methods = getattr(route, "methods", None)
        if methods:
            path = getattr(route, "path", "?")
            assert methods <= {"GET", "HEAD", "OPTIONS"}, f"mutating route: {path} {methods}"


def test_post_is_rejected(client: TestClient) -> None:
    # There is no POST handler anywhere -> 405/404, never a state change.
    assert client.post("/api/orders/recent").status_code in (404, 405)
    assert client.put("/api/equity").status_code in (404, 405)
    assert client.delete("/api/day/2026-06-03").status_code in (404, 405)


def test_dashboard_does_not_import_execution_or_mcp() -> None:
    src = (Path(__file__).resolve().parent.parent / "dashboard" / "api.py").read_text()
    assert "agents.execution" not in src
    assert "RobinhoodMCP" not in src
    assert "place_entry" not in src and "place_exit" not in src


# === endpoints serve real data =============================================


def test_agents_status(client: TestClient) -> None:
    r = client.get("/api/agents/status")
    assert r.status_code == 200
    assert any(a["agent_name"] == "scanner" for a in r.json()["agents"])


def test_equity_series(client: TestClient) -> None:
    r = client.get("/api/equity?range=ALL")
    assert r.status_code == 200
    body = r.json()
    assert body["is_paper"] is True
    assert len(body["points"]) == 1


def test_calendar_and_day(client: TestClient) -> None:
    cal = client.get("/api/calendar?month=2026-06").json()
    assert cal["month"] == "2026-06"
    assert any(d["trading_date"] == "2026-06-03" for d in cal["days"])
    day = client.get("/api/day/2026-06-03").json()
    assert any(t["symbol"] == "AMZN" for t in day["trades"])


def test_pnl_summary_blocks(client: TestClient) -> None:
    s = client.get("/api/pnl/summary").json()
    for key in ("today", "week", "month", "year"):
        assert key in s
    assert s["year"]["total_pnl"] == 12.5  # June trade falls in the current year


def test_positions_and_orders_and_guardrails(client: TestClient) -> None:
    assert client.get("/api/positions/open").status_code == 200
    orders = client.get("/api/orders/recent").json()
    assert orders["orders"][0]["approval_mode"] == "manual"
    g = client.get("/api/guardrails").json()
    assert g["paper_trading"] is True
    assert g["limits"]["max_concurrent_positions"] == 3


def test_index_served(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "Read-Only Dashboard" in r.text


def test_ws_agents_sends_initial_snapshot(client: TestClient) -> None:
    with client.websocket_connect("/ws/agents") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "snapshot"
        assert isinstance(msg["agents"], list)
