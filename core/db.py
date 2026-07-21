"""SQLite persistence layer implementing the Section 4 schema.

SQLite is used per the spec's "Postgres/SQLite" allowance — right for a local,
single-user Phase 1 deployment. Type mapping from the Section 4 DDL:
    UUID/TEXT      -> TEXT
    TIMESTAMPTZ    -> TEXT (ISO-8601 UTC; see :mod:`core.util`)
    NUMERIC        -> REAL
    JSONB          -> TEXT (json.dumps)
    BOOLEAN        -> INTEGER (0/1)

The six Section 4 tables (``trade_journal``, ``calibration_buckets``,
``strategy_config_versions``, ``shadow_test_results``, ``equity_snapshots``,
``daily_pnl``) are created verbatim in intent. Four additional tables required by
other spec sections are also created: ``signal_history`` (Section 2/Section 6 step 2:
every research/aggregator output is persisted even when no trade results),
``orders`` (Section 7.5 ``/api/orders/recent`` display mirror), ``agent_status``
(Section 7.1 "persist the last event per agent"), and ``model_failover_events``
(Section 3.8 failover audit trail).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from config import settings
from core.records import Position
from core.util import new_uuid, utcnow_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_config_versions (
    id TEXT PRIMARY KEY,
    created_ts TEXT NOT NULL,
    parameters TEXT NOT NULL,            -- JSON: full snapshot of all tunable params
    promoted_by TEXT NOT NULL,           -- 'human_confirmed' always in Phase 1
    proposed_by_agent TEXT,
    evidence TEXT,                       -- JSON: z-scores, sample sizes
    is_active INTEGER NOT NULL DEFAULT 0,
    is_shadow INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS trade_journal (
    trade_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    entry_ts TEXT NOT NULL,
    entry_price REAL NOT NULL,
    position_size_usd REAL NOT NULL,
    shares REAL NOT NULL,
    contributing_agents TEXT,            -- JSON {agent: {raw_signal, confidence}}
    aggregated_confidence REAL,
    account_equity_at_entry REAL,
    atr_pct_at_entry REAL,
    market_regime_at_entry TEXT,
    stop_loss_pct REAL NOT NULL,
    take_profit_pct REAL NOT NULL,
    exit_ts TEXT,
    exit_price REAL,
    exit_reason TEXT,
    realized_pnl REAL,
    holding_period_hours REAL,
    config_version_id TEXT REFERENCES strategy_config_versions(id),
    active_model TEXT,
    decided_under_failover INTEGER NOT NULL DEFAULT 0,
    is_paper INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_trade_journal_open ON trade_journal (exit_ts);

CREATE TABLE IF NOT EXISTS calibration_buckets (
    bucket_id TEXT PRIMARY KEY,
    source_agent TEXT NOT NULL,
    confidence_band TEXT NOT NULL,       -- e.g. '0.65-0.70'
    window_start TEXT,
    window_end TEXT,
    sample_size INTEGER NOT NULL,
    wins INTEGER NOT NULL,
    observed_hit_rate REAL,
    expected_hit_rate REAL,
    z_score REAL
);

CREATE TABLE IF NOT EXISTS shadow_test_results (
    id TEXT PRIMARY KEY,
    config_version_id TEXT REFERENCES strategy_config_versions(id),
    start_ts TEXT,
    end_ts TEXT,
    trades_count INTEGER NOT NULL DEFAULT 0,
    sharpe_ratio REAL,
    hit_rate REAL,
    promoted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    total_equity REAL,                   -- cash + marked-to-market open positions
    settled_cash REAL,
    open_positions_value REAL,
    high_water_mark REAL,
    open_position_count INTEGER,
    is_paper INTEGER NOT NULL DEFAULT 1, -- true while PAPER_TRADING = True
    source TEXT                          -- 'monitor_tick' / 'post_trade' / 'scheduled_close'
);
CREATE INDEX IF NOT EXISTS idx_equity_snapshots_ts ON equity_snapshots (ts);

CREATE TABLE IF NOT EXISTS daily_pnl (
    trading_date TEXT PRIMARY KEY,       -- YYYY-MM-DD
    starting_equity REAL,
    ending_equity REAL,
    realized_pnl REAL,
    unrealized_pnl_change REAL,
    total_pnl REAL,
    total_pnl_pct REAL,
    trades_opened INTEGER NOT NULL DEFAULT 0,
    trades_closed INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    is_paper INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS signal_history (
    signal_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    source_agent TEXT NOT NULL,
    direction TEXT,
    magnitude REAL,
    raw_confidence REAL,
    calibrated_confidence REAL,
    reasoning TEXT,
    active_model TEXT,
    decided_under_failover INTEGER NOT NULL DEFAULT 0,
    market_regime TEXT,
    resulted_in_trade INTEGER NOT NULL DEFAULT 0,
    trade_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_signal_history_ts ON signal_history (ts);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,                  -- 'buy' / 'sell'
    quantity REAL,                       -- shares (fractional allowed)
    notional_usd REAL,
    estimated_price REAL,
    status TEXT NOT NULL,                -- staged/submitted_for_approval/blocked/filled/...
    approval_mode TEXT,
    is_paper INTEGER NOT NULL DEFAULT 1,
    reason TEXT,
    trade_id TEXT,
    broker_ref TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders (ts);

CREATE TABLE IF NOT EXISTS agent_status (
    agent_name TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    summary TEXT,
    active_model TEXT,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_failover_events (
    event_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    requested_model TEXT NOT NULL,
    fell_back_to TEXT NOT NULL,
    reason TEXT
);
"""


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    # sqlite3.Row iterates values, not keys, so .keys() is required (not a dict).
    return {k: row[k] for k in row.keys()}  # noqa: SIM118


class Database:
    """Thin typed wrapper over a SQLite connection with domain persistence methods."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def connect(cls, db_path: str | Path | None = None) -> Database:
        """Open (creating parent dirs) a database at ``db_path`` or the configured path.

        Pass ``":memory:"`` for an ephemeral in-memory database (used by tests).
        """
        path = str(db_path) if db_path is not None else str(settings.DB_PATH)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        db = cls(conn)
        db.init_schema()
        return db

    def init_schema(self) -> None:
        """Create all tables and indexes if they do not already exist."""
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- strategy_config_versions -----------------------------------------

    def insert_config_version(
        self,
        parameters: dict[str, Any],
        promoted_by: str,
        proposed_by_agent: str | None = None,
        evidence: dict[str, Any] | None = None,
        is_active: bool = False,
        is_shadow: bool = False,
    ) -> str:
        cid = new_uuid()
        self.conn.execute(
            """INSERT INTO strategy_config_versions
               (id, created_ts, parameters, promoted_by, proposed_by_agent, evidence,
                is_active, is_shadow)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                cid,
                utcnow_iso(),
                json.dumps(parameters),
                promoted_by,
                proposed_by_agent,
                json.dumps(evidence) if evidence is not None else None,
                int(is_active),
                int(is_shadow),
            ),
        )
        self.conn.commit()
        return cid

    def set_active_config(self, config_version_id: str) -> None:
        """Mark exactly one live (non-shadow) config version active."""
        self.conn.execute("UPDATE strategy_config_versions SET is_active = 0 WHERE is_shadow = 0")
        self.conn.execute(
            "UPDATE strategy_config_versions SET is_active = 1 WHERE id = ?",
            (config_version_id,),
        )
        self.conn.commit()

    def active_config(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM strategy_config_versions WHERE is_active = 1 AND is_shadow = 0 "
            "ORDER BY created_ts DESC LIMIT 1"
        ).fetchone()
        return _row_to_dict(row) if row else None

    def get_config_version(self, config_version_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM strategy_config_versions WHERE id = ?", (config_version_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None

    # -- trade_journal -----------------------------------------------------

    def insert_trade_entry(
        self,
        *,
        symbol: str,
        entry_price: float,
        position_size_usd: float,
        shares: float,
        contributing_agents: dict[str, Any],
        aggregated_confidence: float,
        account_equity_at_entry: float,
        atr_pct_at_entry: float,
        market_regime_at_entry: str,
        stop_loss_pct: float,
        take_profit_pct: float,
        config_version_id: str | None,
        active_model: str,
        decided_under_failover: bool = False,
        is_paper: bool = True,
        entry_ts: str | None = None,
    ) -> str:
        trade_id = new_uuid()
        self.conn.execute(
            """INSERT INTO trade_journal
               (trade_id, symbol, entry_ts, entry_price, position_size_usd, shares,
                contributing_agents, aggregated_confidence, account_equity_at_entry,
                atr_pct_at_entry, market_regime_at_entry, stop_loss_pct, take_profit_pct,
                config_version_id, active_model, decided_under_failover, is_paper)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade_id,
                symbol,
                entry_ts or utcnow_iso(),
                entry_price,
                position_size_usd,
                shares,
                json.dumps(contributing_agents),
                aggregated_confidence,
                account_equity_at_entry,
                atr_pct_at_entry,
                market_regime_at_entry,
                stop_loss_pct,
                take_profit_pct,
                config_version_id,
                active_model,
                int(decided_under_failover),
                int(is_paper),
            ),
        )
        self.conn.commit()
        return trade_id

    def close_trade(
        self,
        trade_id: str,
        *,
        exit_price: float,
        exit_reason: str,
        realized_pnl: float,
        holding_period_hours: float,
        exit_ts: str | None = None,
    ) -> None:
        self.conn.execute(
            """UPDATE trade_journal
               SET exit_ts = ?, exit_price = ?, exit_reason = ?, realized_pnl = ?,
                   holding_period_hours = ?
               WHERE trade_id = ?""",
            (
                exit_ts or utcnow_iso(),
                exit_price,
                exit_reason,
                realized_pnl,
                holding_period_hours,
                trade_id,
            ),
        )
        self.conn.commit()

    def open_positions(self) -> list[Position]:
        rows = self.conn.execute(
            "SELECT * FROM trade_journal WHERE exit_ts IS NULL ORDER BY entry_ts"
        ).fetchall()
        return [
            Position(
                trade_id=r["trade_id"],
                symbol=r["symbol"],
                entry_price=r["entry_price"],
                shares=r["shares"],
                position_size_usd=r["position_size_usd"],
                entry_ts=r["entry_ts"],
                stop_loss_pct=r["stop_loss_pct"],
                take_profit_pct=r["take_profit_pct"],
            )
            for r in rows
        ]

    def open_position_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM trade_journal WHERE exit_ts IS NULL"
        ).fetchone()
        return int(row["c"])

    def closed_trades(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM trade_journal WHERE exit_ts IS NOT NULL ORDER BY exit_ts"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def all_trades(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM trade_journal ORDER BY entry_ts").fetchall()
        return [_row_to_dict(r) for r in rows]

    def trades_on_date(self, trading_date: str) -> list[dict[str, Any]]:
        """Trades opened or closed on the given YYYY-MM-DD (UTC)."""
        rows = self.conn.execute(
            "SELECT * FROM trade_journal WHERE substr(entry_ts,1,10) = ? "
            "OR substr(exit_ts,1,10) = ? ORDER BY entry_ts",
            (trading_date, trading_date),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # -- signal_history ----------------------------------------------------

    def insert_signal(
        self,
        *,
        symbol: str,
        source_agent: str,
        direction: str,
        magnitude: float,
        raw_confidence: float,
        calibrated_confidence: float,
        reasoning: str,
        active_model: str,
        decided_under_failover: bool = False,
        market_regime: str | None = None,
        resulted_in_trade: bool = False,
        trade_id: str | None = None,
        ts: str | None = None,
    ) -> str:
        signal_id = new_uuid()
        self.conn.execute(
            """INSERT INTO signal_history
               (signal_id, ts, symbol, source_agent, direction, magnitude, raw_confidence,
                calibrated_confidence, reasoning, active_model, decided_under_failover,
                market_regime, resulted_in_trade, trade_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                signal_id,
                ts or utcnow_iso(),
                symbol,
                source_agent,
                direction,
                magnitude,
                raw_confidence,
                calibrated_confidence,
                reasoning,
                active_model,
                int(decided_under_failover),
                market_regime,
                int(resulted_in_trade),
                trade_id,
            ),
        )
        self.conn.commit()
        return signal_id

    def mark_signal_traded(self, symbol: str, trade_id: str) -> None:
        """Mark the most recent aggregator signal for a symbol as having led to a trade."""
        row = self.conn.execute(
            "SELECT signal_id FROM signal_history WHERE symbol = ? AND source_agent = "
            "'edge_aggregator' ORDER BY ts DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        if row is not None:
            self.conn.execute(
                "UPDATE signal_history SET resulted_in_trade = 1, trade_id = ? WHERE signal_id = ?",
                (trade_id, row["signal_id"]),
            )
            self.conn.commit()

    def recent_signals(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM signal_history ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # -- orders (display mirror) -------------------------------------------

    def insert_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        notional_usd: float,
        estimated_price: float,
        status: str,
        approval_mode: str,
        is_paper: bool = True,
        reason: str | None = None,
        trade_id: str | None = None,
        broker_ref: str | None = None,
        ts: str | None = None,
    ) -> str:
        order_id = new_uuid()
        self.conn.execute(
            """INSERT INTO orders
               (order_id, ts, symbol, side, quantity, notional_usd, estimated_price, status,
                approval_mode, is_paper, reason, trade_id, broker_ref)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                order_id,
                ts or utcnow_iso(),
                symbol,
                side,
                quantity,
                notional_usd,
                estimated_price,
                status,
                approval_mode,
                int(is_paper),
                reason,
                trade_id,
                broker_ref,
            ),
        )
        self.conn.commit()
        return order_id

    def update_order_status(
        self, order_id: str, status: str, reason: str | None = None
    ) -> None:
        self.conn.execute(
            "UPDATE orders SET status = ?, reason = COALESCE(?, reason) WHERE order_id = ?",
            (status, reason, order_id),
        )
        self.conn.commit()

    def recent_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM orders ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # -- equity_snapshots --------------------------------------------------

    def insert_equity_snapshot(
        self,
        *,
        total_equity: float,
        settled_cash: float,
        open_positions_value: float,
        high_water_mark: float,
        open_position_count: int,
        source: str,
        is_paper: bool = True,
        ts: str | None = None,
    ) -> str:
        snapshot_id = new_uuid()
        self.conn.execute(
            """INSERT INTO equity_snapshots
               (snapshot_id, ts, total_equity, settled_cash, open_positions_value,
                high_water_mark, open_position_count, is_paper, source)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                snapshot_id,
                ts or utcnow_iso(),
                total_equity,
                settled_cash,
                open_positions_value,
                high_water_mark,
                open_position_count,
                int(is_paper),
                source,
            ),
        )
        self.conn.commit()
        return snapshot_id

    def equity_series(
        self, start_ts: str | None = None, is_paper: bool = True
    ) -> list[dict[str, Any]]:
        if start_ts is None:
            rows = self.conn.execute(
                "SELECT * FROM equity_snapshots WHERE is_paper = ? ORDER BY ts",
                (int(is_paper),),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM equity_snapshots WHERE is_paper = ? AND ts >= ? ORDER BY ts",
                (int(is_paper), start_ts),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def latest_high_water_mark(self, is_paper: bool = True) -> float | None:
        row = self.conn.execute(
            "SELECT MAX(high_water_mark) AS hwm FROM equity_snapshots WHERE is_paper = ?",
            (int(is_paper),),
        ).fetchone()
        return row["hwm"] if row and row["hwm"] is not None else None

    # -- daily_pnl ---------------------------------------------------------

    def upsert_daily_pnl(
        self,
        *,
        trading_date: str,
        starting_equity: float,
        ending_equity: float,
        realized_pnl: float,
        unrealized_pnl_change: float,
        total_pnl: float,
        total_pnl_pct: float,
        trades_opened: int,
        trades_closed: int,
        wins: int,
        losses: int,
        is_paper: bool = True,
    ) -> None:
        self.conn.execute(
            """INSERT INTO daily_pnl
               (trading_date, starting_equity, ending_equity, realized_pnl,
                unrealized_pnl_change, total_pnl, total_pnl_pct, trades_opened,
                trades_closed, wins, losses, is_paper)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(trading_date) DO UPDATE SET
                   starting_equity=excluded.starting_equity,
                   ending_equity=excluded.ending_equity,
                   realized_pnl=excluded.realized_pnl,
                   unrealized_pnl_change=excluded.unrealized_pnl_change,
                   total_pnl=excluded.total_pnl,
                   total_pnl_pct=excluded.total_pnl_pct,
                   trades_opened=excluded.trades_opened,
                   trades_closed=excluded.trades_closed,
                   wins=excluded.wins,
                   losses=excluded.losses,
                   is_paper=excluded.is_paper""",
            (
                trading_date,
                starting_equity,
                ending_equity,
                realized_pnl,
                unrealized_pnl_change,
                total_pnl,
                total_pnl_pct,
                trades_opened,
                trades_closed,
                wins,
                losses,
                int(is_paper),
            ),
        )
        self.conn.commit()

    def daily_pnl_range(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM daily_pnl WHERE trading_date >= ? AND trading_date <= ? "
            "ORDER BY trading_date",
            (start_date, end_date),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def daily_pnl_month(self, year: int, month: int) -> list[dict[str, Any]]:
        prefix = f"{year:04d}-{month:02d}"
        rows = self.conn.execute(
            "SELECT * FROM daily_pnl WHERE substr(trading_date,1,7) = ? ORDER BY trading_date",
            (prefix,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # -- calibration_buckets ----------------------------------------------

    def insert_calibration_bucket(
        self,
        *,
        source_agent: str,
        confidence_band: str,
        window_start: str | None,
        window_end: str | None,
        sample_size: int,
        wins: int,
        observed_hit_rate: float,
        expected_hit_rate: float,
        z_score: float,
    ) -> str:
        bucket_id = new_uuid()
        self.conn.execute(
            """INSERT INTO calibration_buckets
               (bucket_id, source_agent, confidence_band, window_start, window_end,
                sample_size, wins, observed_hit_rate, expected_hit_rate, z_score)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                bucket_id,
                source_agent,
                confidence_band,
                window_start,
                window_end,
                sample_size,
                wins,
                observed_hit_rate,
                expected_hit_rate,
                z_score,
            ),
        )
        self.conn.commit()
        return bucket_id

    def latest_calibration_buckets(self) -> list[dict[str, Any]]:
        """Most recent bucket row per (source_agent, confidence_band)."""
        rows = self.conn.execute(
            """SELECT cb.* FROM calibration_buckets cb
               JOIN (
                   SELECT source_agent, confidence_band, MAX(rowid) AS mx
                   FROM calibration_buckets GROUP BY source_agent, confidence_band
               ) latest
               ON cb.source_agent = latest.source_agent
               AND cb.confidence_band = latest.confidence_band
               AND cb.rowid = latest.mx
               ORDER BY cb.source_agent, cb.confidence_band"""
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # -- shadow_test_results ----------------------------------------------

    def insert_shadow_result(
        self,
        *,
        config_version_id: str,
        start_ts: str,
        end_ts: str,
        trades_count: int,
        sharpe_ratio: float,
        hit_rate: float,
        promoted: bool,
    ) -> str:
        rid = new_uuid()
        self.conn.execute(
            """INSERT INTO shadow_test_results
               (id, config_version_id, start_ts, end_ts, trades_count, sharpe_ratio,
                hit_rate, promoted)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                rid,
                config_version_id,
                start_ts,
                end_ts,
                trades_count,
                sharpe_ratio,
                hit_rate,
                int(promoted),
            ),
        )
        self.conn.commit()
        return rid

    def shadow_results(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM shadow_test_results ORDER BY start_ts"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # -- agent_status ------------------------------------------------------

    def upsert_agent_status(
        self,
        *,
        agent_name: str,
        state: str,
        summary: str | None,
        active_model: str | None,
        ts: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO agent_status (agent_name, state, summary, active_model, ts)
               VALUES (?,?,?,?,?)
               ON CONFLICT(agent_name) DO UPDATE SET
                   state=excluded.state, summary=excluded.summary,
                   active_model=excluded.active_model, ts=excluded.ts""",
            (agent_name, state, summary, active_model, ts or utcnow_iso()),
        )
        self.conn.commit()

    def all_agent_status(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM agent_status ORDER BY agent_name"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # -- model_failover_events --------------------------------------------

    def insert_failover_event(
        self,
        *,
        agent_name: str,
        requested_model: str,
        fell_back_to: str,
        reason: str,
        ts: str | None = None,
    ) -> str:
        event_id = new_uuid()
        self.conn.execute(
            """INSERT INTO model_failover_events
               (event_id, ts, agent_name, requested_model, fell_back_to, reason)
               VALUES (?,?,?,?,?,?)""",
            (event_id, ts or utcnow_iso(), agent_name, requested_model, fell_back_to, reason),
        )
        self.conn.commit()
        return event_id

    def failover_events(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM model_failover_events ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
