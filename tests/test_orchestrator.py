"""Orchestrator + PaperPortfolio tests (end-to-end pipeline in paper mode)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.db import Database
from core.llm import ModelClient, ModelUnavailableError, OfflineProvider
from orchestrator import Orchestrator, PaperPortfolio

NOW = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)


class AlwaysFailoverProvider:
    """Fable is always unreachable; opus always answers -> every call is on failover."""

    kind = "anthropic"

    def generate(self, model: str, system: str, user: str) -> str:
        if model == "claude-fable-5":
            raise ModelUnavailableError("suspended")
        return (
            '{"direction": "long", "magnitude": 0.6, "raw_confidence": 0.8, '
            '"reasoning": "x", "ranked": ["AMZN"], "invalidated": false, "flag": "f", '
            '"rationale": "r", "summary": "s"}'
        )


def build(db: Database, universe: list[str] | None = None, capital: float = 300.0):  # noqa: ANN201
    client = ModelClient(provider=OfflineProvider(), db=db)
    pf = PaperPortfolio(capital)
    orch = Orchestrator(db, client, pf, universe=universe or ["AMZN"])
    return orch, pf


# === PaperPortfolio settlement ============================================


def test_portfolio_settlement_excludes_unsettled_then_settles() -> None:
    pf = PaperPortfolio(100.0)
    pf.on_sell(50.0, settlement_date=NOW + timedelta(days=1))
    # Unsettled proceeds are excluded from deployable settled cash.
    assert pf.settled_available(NOW) == 100.0
    assert pf.unsettled_total() == 50.0
    # After T+1 they settle and become deployable.
    pf.settle(NOW + timedelta(days=1))
    assert pf.settled_available(NOW + timedelta(days=2)) == 150.0
    assert pf.unsettled_total() == 0.0


def test_buy_reduces_settled_cash() -> None:
    pf = PaperPortfolio(300.0)
    pf.on_buy(120.0)
    assert pf.settled_available(NOW) == 180.0


# === entry cycle ===========================================================


def test_entry_cycle_opens_position_and_snapshots(db: Database) -> None:
    orch, _pf = build(db)
    outcomes = orch.run_entry_cycle(NOW, regime="normal")
    assert any(o.get("result") == "opened" for o in outcomes)
    assert db.open_position_count() == 1
    # Equity snapshot + a config version attached to the trade.
    assert db.equity_series()
    trade = db.all_trades()[0]
    assert trade["config_version_id"] is not None
    assert trade["is_paper"] == 1
    # The aggregator signal was marked as having led to a trade.
    agg_sigs = [s for s in db.recent_signals() if s["source_agent"] == "edge_aggregator"]
    assert any(s["resulted_in_trade"] == 1 for s in agg_sigs)


def test_no_new_entry_when_no_settled_cash(db: Database) -> None:
    # Start with essentially no capital -> sizing returns 0 -> no position opened.
    orch, _pf = build(db, capital=100.0)
    orch.portfolio.settled_cash = 0.0
    outcomes = orch.run_entry_cycle(NOW, regime="normal")
    assert db.open_position_count() == 0
    assert all(o.get("result") != "opened" for o in outcomes)


# === monitor / exit cycle ==================================================


def test_monitor_closes_on_stop_and_proceeds_are_unsettled(db: Database) -> None:
    orch, pf = build(db)
    orch.run_entry_cycle(NOW, regime="normal")
    pos = db.open_positions()[0]
    stop_price = pos.entry_price * (1 - 0.20)  # beyond the 18% hard stop

    exits = orch.run_monitor_cycle(lambda _s: stop_price, NOW + timedelta(days=1), regime="normal")
    assert exits and exits[0]["reason"] == "stop_loss"
    assert db.open_position_count() == 0
    closed = db.closed_trades()
    assert closed[0]["exit_reason"] == "stop_loss"
    assert closed[0]["realized_pnl"] < 0
    # Sale proceeds are unsettled until T+1 (GFV avoidance).
    assert pf.unsettled_total() > 0
    assert pf.settled_available(NOW + timedelta(days=1)) == pf.settled_cash


def test_monitor_take_profit_exit(db: Database) -> None:
    orch, _pf = build(db)
    orch.run_entry_cycle(NOW, regime="normal")
    pos = db.open_positions()[0]
    tp_price = pos.entry_price * 1.30  # beyond the 25% take-profit
    exits = orch.run_monitor_cycle(lambda _s: tp_price, NOW + timedelta(days=1), regime="normal")
    assert exits and exits[0]["reason"] == "take_profit"
    assert db.closed_trades()[0]["realized_pnl"] > 0


# === Section 3.8 failover policy ===========================================


def test_new_entries_disabled_under_failover(db: Database) -> None:
    client = ModelClient(provider=AlwaysFailoverProvider(), db=db)
    orch = Orchestrator(db, client, PaperPortfolio(300.0), universe=["AMZN"])
    outcomes = orch.run_entry_cycle(NOW, regime="normal")
    # Every decision was made on a fallback model -> no new capital committed.
    assert db.open_position_count() == 0
    assert any(o.get("result") == "skipped_failover" for o in outcomes)
    # The failover was recorded to the audit trail.
    assert db.failover_events()


# === daily rollup ==========================================================


def test_daily_pnl_rollup_written(db: Database) -> None:
    orch, _pf = build(db)
    orch.run_entry_cycle(NOW, regime="normal")
    orch.rollup_daily_pnl("2026-06-01", lambda s: 100.0, NOW)
    rows = db.daily_pnl_range("2026-06-01", "2026-06-01")
    assert len(rows) == 1
    assert rows[0]["trades_opened"] >= 1
