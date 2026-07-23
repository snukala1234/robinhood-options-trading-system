"""Structural read-only: SELECT-only role, import isolation, localhost bind."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import errors
from psycopg.rows import dict_row

from src.config.environments import ConfigurationError
from src.dashboard.app import create_app, serve
from src.dashboard.readonly_db import (
    ensure_readonly_role,
    readonly_connection,
    readonly_url,
)

SRC = Path(__file__).resolve().parents[2] / "src"

WRITE_ATTEMPTS = (
    "INSERT INTO system_events (id, created_at, severity, component, event_type, "
    "correlation_id, payload) VALUES (gen_random_uuid(), now(), 'info', 'x', 'x', NULL, '{}')",
    "UPDATE trade_proposals SET approval_status = 'hacked'",
    "UPDATE orders SET current_state = 'FILLED'",
    "DELETE FROM order_events",
    "TRUNCATE system_events",
    "CREATE TABLE exfil (x int)",
    "DROP TABLE system_events",
    "ALTER TABLE orders DISABLE TRIGGER ALL",
)


@pytest.fixture(scope="module")
def ro_url(migrated: str) -> str:
    with psycopg.connect(migrated, autocommit=True, row_factory=dict_row) as admin:
        ensure_readonly_role(admin, password="ro-secret")
    return readonly_url(migrated, password="ro-secret")


def test_dashboard_role_can_read(ro_url: str) -> None:
    with readonly_connection(ro_url) as conn:
        row = conn.execute("SELECT 1 AS one").fetchone()
        assert row is not None and row["one"] == 1
        for table in ("orders", "positions", "trade_proposals", "system_events"):
            count = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
            assert count is not None and count["n"] >= 0


@pytest.mark.parametrize("statement", WRITE_ATTEMPTS)
def test_dashboard_role_cannot_write_even_via_raw_sql(ro_url: str, statement: str) -> None:
    """Not convention — privilege. Raw SQL as the dashboard role dies at the
    database, so no bug in a route handler can mutate trading state."""
    with (
        readonly_connection(ro_url) as conn,
        pytest.raises((errors.InsufficientPrivilege, errors.ReadOnlySqlTransaction)),
    ):
        conn.execute(statement)


def test_dashboard_package_imports_no_trading_code() -> None:
    """The same sweep pattern as agents/learning: no route handler can reach
    the gate, the submitter, a broker, the agents, or the analytics engines."""
    forbidden = (
        "src.execution",
        "src.gate",
        "src.agents",
        "src.positions",
        "src.learning",
        "src.risk",
        "src.analytics",
        "src.data",
        "broker",
        "anthropic",
    )
    offenders: list[str] = []
    for path in (SRC / "dashboard").rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            if any(term in stripped for term in forbidden):
                offenders.append(f"{path.name}: {stripped}")
    assert offenders == []


def test_credit_strategy_set_stays_in_sync_with_execution() -> None:
    """The dashboard duplicates the tiny credit-strategy set instead of
    importing execution code; this test pins the two together."""
    from src.dashboard.panels import _CREDIT_STRATEGIES as dashboard_set
    from src.gate.trade_gate import _CREDIT_STRATEGIES as gate_set

    assert dashboard_set == gate_set


def test_serve_refuses_non_local_hosts() -> None:
    @contextmanager
    def provider() -> Any:
        yield None

    app = create_app(provider)
    with pytest.raises(ConfigurationError, match="localhost only"):
        serve(app, host="0.0.0.0")
    with pytest.raises(ConfigurationError, match="localhost only"):
        serve(app, host="192.168.1.10")


def test_readonly_url_refuses_remote_hosts() -> None:
    with pytest.raises(ConfigurationError, match="not local"):
        readonly_url("postgresql://user:pw@db.example.com:5432/options_v2", password="x")
