"""Section 14 schema on real PostgreSQL: tables, native types, reversible migrations."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from psycopg.rows import dict_row

EXPECTED_TABLES = {
    "broker_capability_snapshots",
    "market_data_snapshots",
    "option_contract_snapshots",
    "strategy_config_versions",
    "opportunity_candidates",
    "trade_proposals",
    "orders",
    "order_events",
    "positions",
    "position_snapshots",
    "portfolio_snapshots",
    "agent_decisions",
    "calibration_results",
    "system_events",
}


def _tables(conn: psycopg.Connection[Any]) -> set[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
    ).fetchall()
    return {str(r["table_name"]) for r in rows} - {"alembic_version"}


def _column_types(conn: psycopg.Connection[Any], table: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    ).fetchall()
    return {str(r["column_name"]): str(r["data_type"]) for r in rows}


def test_all_fourteen_tables_exist(conn: psycopg.Connection[Any]) -> None:
    assert _tables(conn) == EXPECTED_TABLES


def test_native_postgres_types_no_dialect_lowering(
    conn: psycopg.Connection[Any],
) -> None:
    """UUID/TIMESTAMPTZ/JSONB/NUMERIC must be native - never TEXT/REAL stand-ins."""
    ocs = _column_types(conn, "option_contract_snapshots")
    assert ocs["id"] == "uuid"
    assert ocs["observed_at"] == "timestamp with time zone"
    assert ocs["raw_payload"] == "jsonb"
    assert ocs["expiration"] == "date"
    for money_col in (
        "strike",
        "bid",
        "ask",
        "midpoint",
        "implied_volatility",
        "delta",
        "gamma",
        "theta",
        "vega",
    ):
        assert ocs[money_col] == "numeric", money_col
    assert ocs["volume"] == "bigint"

    positions = _column_types(conn, "positions")
    for money_col in ("entry_net_price", "exit_net_price", "max_loss"):
        assert positions[money_col] == "numeric", money_col
    assert positions["legs"] == "jsonb"
    assert positions["opened_at"] == "timestamp with time zone"

    portfolio = _column_types(conn, "portfolio_snapshots")
    for money_col in (
        "total_equity",
        "settled_cash",
        "unsettled_cash",
        "open_risk",
        "net_delta",
        "net_gamma",
        "daily_theta",
        "net_vega",
        "high_water_mark",
        "drawdown",
    ):
        assert portfolio[money_col] == "numeric", money_col
    assert portfolio["is_paper"] == "boolean"

    decisions = _column_types(conn, "agent_decisions")
    assert decisions["correlation_id"] == "uuid"
    assert decisions["input_snapshot_ids"] == "jsonb"


def test_orders_idempotency_key_is_unique(
    conn: psycopg.Connection[Any],
) -> None:
    row = conn.execute(
        """SELECT COUNT(*) AS n FROM information_schema.table_constraints
           WHERE table_name = 'orders' AND constraint_type = 'UNIQUE'"""
    ).fetchone()
    assert row is not None and int(row["n"]) >= 1


def test_migrations_are_reversible(migrated: str, alembic_cfg: AlembicConfig) -> None:
    """Downgrade to base removes every table; upgrade restores all fourteen."""
    command.downgrade(alembic_cfg, "base")
    try:
        with psycopg.connect(migrated, row_factory=dict_row) as c:
            assert _tables(c) == set()
    finally:
        command.upgrade(alembic_cfg, "head")
    with psycopg.connect(migrated, row_factory=dict_row) as c:
        assert _tables(c) == EXPECTED_TABLES


def test_no_create_table_outside_migrations() -> None:
    """Schema management belongs to Alembic alone (Phase B requirement 4)."""
    from pathlib import Path

    src_root = Path(__file__).resolve().parents[2] / "src"
    offenders = [
        str(p)
        for p in src_root.rglob("*.py")
        if "migrations" not in p.parts and "CREATE TABLE" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


@pytest.mark.parametrize("bad_url", ["postgresql://u:p@db.remote.host:5432/x"])
def test_db_connect_refuses_remote_hosts(bad_url: str) -> None:
    from src.config.environments import ConfigurationError
    from src.persistence import db

    with pytest.raises(ConfigurationError):
        db.connect(bad_url)
