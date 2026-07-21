"""Tests for the persistence, market-data, and event-bus foundation (Component 1)."""

from __future__ import annotations

import asyncio

from core.db import SCHEMA, Database
from core.event_bus import EventBus, publish_agent_status
from core.market_data import get_snapshot, get_snapshots
from core.records import MarketSnapshot

# -- schema -----------------------------------------------------------------

def test_all_six_section4_tables_plus_support_tables_exist(db: Database) -> None:
    rows = db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {r["name"] for r in rows}
    section4 = {
        "trade_journal",
        "calibration_buckets",
        "strategy_config_versions",
        "shadow_test_results",
        "equity_snapshots",
        "daily_pnl",
    }
    assert section4.issubset(names)
    # Spec-mandated support tables.
    for extra in ("signal_history", "orders", "agent_status", "model_failover_events"):
        assert extra in names


def test_schema_is_idempotent(db: Database) -> None:
    # Re-running the schema must not error (IF NOT EXISTS everywhere).
    db.conn.executescript(SCHEMA)


# -- trade lifecycle --------------------------------------------------------

def test_trade_entry_and_close_roundtrip(db: Database) -> None:
    cid = db.insert_config_version({"k": 1}, promoted_by="human_confirmed", is_active=True)
    trade_id = db.insert_trade_entry(
        symbol="AAPL",
        entry_price=100.0,
        position_size_usd=50.0,
        shares=0.5,
        contributing_agents={"research_technical": {"raw_signal": "long", "confidence": 0.7}},
        aggregated_confidence=0.72,
        account_equity_at_entry=200.0,
        atr_pct_at_entry=0.02,
        market_regime_at_entry="normal",
        stop_loss_pct=0.18,
        take_profit_pct=0.25,
        config_version_id=cid,
        active_model="claude-fable-5",
    )
    assert db.open_position_count() == 1
    positions = db.open_positions()
    assert positions[0].symbol == "AAPL"

    db.close_trade(
        trade_id,
        exit_price=110.0,
        exit_reason="take_profit",
        realized_pnl=5.0,
        holding_period_hours=6.0,
    )
    assert db.open_position_count() == 0
    closed = db.closed_trades()
    assert len(closed) == 1 and closed[0]["exit_reason"] == "take_profit"


def test_signal_history_persists_without_a_trade(db: Database) -> None:
    # Section 6 step 2: every research output is persisted even when no trade results.
    db.insert_signal(
        symbol="MSFT",
        source_agent="research_technical",
        direction="long",
        magnitude=0.6,
        raw_confidence=0.8,
        calibrated_confidence=0.68,
        reasoning="uptrend intact",
        active_model="claude-fable-5",
        resulted_in_trade=False,
    )
    signals = db.recent_signals()
    assert len(signals) == 1
    assert signals[0]["resulted_in_trade"] == 0


def test_equity_snapshot_and_daily_pnl(db: Database) -> None:
    db.insert_equity_snapshot(
        total_equity=205.0,
        settled_cash=150.0,
        open_positions_value=55.0,
        high_water_mark=205.0,
        open_position_count=1,
        source="monitor_tick",
    )
    series = db.equity_series()
    assert len(series) == 1 and series[0]["total_equity"] == 205.0
    assert db.latest_high_water_mark() == 205.0

    db.upsert_daily_pnl(
        trading_date="2026-07-05",
        starting_equity=200.0,
        ending_equity=205.0,
        realized_pnl=0.0,
        unrealized_pnl_change=5.0,
        total_pnl=5.0,
        total_pnl_pct=0.025,
        trades_opened=1,
        trades_closed=0,
        wins=0,
        losses=0,
    )
    # Upsert again for same date must overwrite, not duplicate.
    db.upsert_daily_pnl(
        trading_date="2026-07-05",
        starting_equity=200.0,
        ending_equity=210.0,
        realized_pnl=0.0,
        unrealized_pnl_change=10.0,
        total_pnl=10.0,
        total_pnl_pct=0.05,
        trades_opened=1,
        trades_closed=0,
        wins=0,
        losses=0,
    )
    rows = db.daily_pnl_range("2026-07-01", "2026-07-31")
    assert len(rows) == 1 and rows[0]["ending_equity"] == 210.0


def test_agent_status_upsert_keeps_one_row_per_agent(db: Database) -> None:
    db.upsert_agent_status(agent_name="scanner", state="idle", summary=None, active_model=None)
    db.upsert_agent_status(
        agent_name="scanner", state="running", summary="scanning", active_model="claude-fable-5"
    )
    rows = db.all_agent_status()
    assert len(rows) == 1 and rows[0]["state"] == "running"


def test_failover_event_audit(db: Database) -> None:
    db.insert_failover_event(
        agent_name="research_macro",
        requested_model="claude-fable-5",
        fell_back_to="claude-opus-4-8",
        reason="sustained_unavailable",
    )
    events = db.failover_events()
    assert len(events) == 1 and events[0]["fell_back_to"] == "claude-opus-4-8"


# -- market data ------------------------------------------------------------

def test_offline_snapshot_is_deterministic() -> None:
    a = get_snapshot("AAPL")
    b = get_snapshot("AAPL")
    assert isinstance(a, MarketSnapshot)
    # Deterministic offline fixture: every field except the real-time fetch stamp.
    assert (a.symbol, a.current_price, a.atr_14, a.atr_pct, a.volume, a.avg_volume,
            a.volume_ratio) == (b.symbol, b.current_price, b.atr_14, b.atr_pct,
                                b.volume, b.avg_volume, b.volume_ratio)
    assert a.current_price > 0 and a.atr_14 > 0 and 0 < a.atr_pct < 0.1


def test_get_snapshots_covers_all_symbols() -> None:
    snaps = get_snapshots(["AAPL", "MSFT", "NVDA"])
    assert set(snaps.keys()) == {"AAPL", "MSFT", "NVDA"}


# -- event bus --------------------------------------------------------------

def test_event_bus_sync_subscribe_and_history() -> None:
    bus = EventBus()
    received: list[dict] = []
    bus.subscribe(received.append)
    bus.publish({"type": "agent_status", "agent_name": "scanner", "state": "running"})
    assert len(received) == 1
    assert bus.history()[-1]["agent_name"] == "scanner"


def test_event_bus_subscriber_exception_is_swallowed() -> None:
    bus = EventBus()

    def boom(_event: dict) -> None:
        raise ValueError("subscriber failure must not break publish")

    bus.subscribe(boom)
    # Must not raise.
    bus.publish({"type": "x"})
    assert len(bus.history()) == 1


def test_publish_agent_status_helper_uses_singleton() -> None:
    event = publish_agent_status("scanner", "idle")
    assert event["type"] == "agent_status" and event["state"] == "idle"


def test_event_bus_async_queue_delivery() -> None:
    async def scenario() -> None:
        bus = EventBus()
        loop = asyncio.get_running_loop()
        bus.bind_loop(loop)
        queue = bus.async_subscribe()
        bus.publish({"type": "equity", "total_equity": 200.0})
        # call_soon_threadsafe schedules delivery; let the loop run once.
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert event["total_equity"] == 200.0

    asyncio.run(scenario())
