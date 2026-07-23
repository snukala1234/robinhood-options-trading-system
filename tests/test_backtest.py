"""Backtest harness tests: no look-ahead, OHLC exits, slippage, T+1, determinism."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

from agents.exit_monitor import REASON_STOP_LOSS
from backtest.data import HistoricalBarStore
from backtest.engine import BacktestConfig, BacktestEngine
from config.guardrails import HARD_STOP_LOSS_PCT
from config.strategy import DEFAULT_STRATEGY


def make_store(
    paths: dict[str, list[tuple[float, float, float, float]]], start: str = "2024-01-02"
) -> HistoricalBarStore:
    n = len(next(iter(paths.values())))
    dates = pd.bdate_range(start, periods=n)
    bars: dict[str, pd.DataFrame] = {}
    for sym, rows in paths.items():
        df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=dates)
        df["Volume"] = 1_000_000.0
        bars[sym] = df
    return HistoricalBarStore(bars, calendar_symbol="SPY")


def flat(price: float, n: int) -> list[tuple[float, float, float, float]]:
    return [(price, price * 1.001, price * 0.999, price)] * n


# === no look-ahead: snapshot only uses bars <= as-of date ==================


def test_snapshot_asof_ignores_future_bars() -> None:
    rows = [(100.0 + i, 101.0 + i, 99.0 + i, 100.0 + i) for i in range(40)]
    store = make_store({"AAA": rows, "SPY": flat(400, 40)})
    dates = store.bars["AAA"].index
    snap1 = store.snapshot_asof("AAA", dates[20])
    assert snap1 is not None
    # Poison ALL future bars with absurd values.
    store.bars["AAA"].iloc[21:] = 1_000_000.0
    snap2 = store.snapshot_asof("AAA", dates[20])
    assert snap2 is not None
    # The as-of-day-20 snapshot must be unchanged — it never looked past day 20.
    assert (snap1.current_price, snap1.atr_14, snap1.volume_ratio) == (
        snap2.current_price,
        snap2.atr_14,
        snap2.volume_ratio,
    )
    # And current price is exactly day-20's close, not any later bar.
    assert snap1.current_price == float(store.bars["AAA"]["Close"].iloc[20])


def test_bar_on_returns_exactly_that_day() -> None:
    rows = [(10.0 + i, 11.0 + i, 9.0 + i, 10.5 + i) for i in range(30)]
    store = make_store({"AAA": rows, "SPY": flat(400, 30)})
    d = store.bars["AAA"].index[15]
    bar = store.bar_on("AAA", d)
    assert bar is not None
    assert bar.close == 10.5 + 15 and bar.high == 11.0 + 15 and bar.low == 9.0 + 15


# === no look-ahead: exits use ONLY that day's OHLC =========================


def _engine_with_open_position(store: HistoricalBarStore) -> tuple[BacktestEngine, str]:
    cfg = BacktestConfig(
        universe=["AAA"],
        start="x",
        end="y",
        starting_capital=1000.0,
        slippage_per_side=0.0005,
        db_path=":memory:",
    )
    eng = BacktestEngine(cfg, store=store)
    eng.portfolio.on_buy(100.0)
    trade_id = eng.db.insert_trade_entry(
        symbol="AAA",
        entry_price=100.0,
        position_size_usd=100.0,
        shares=1.0,
        contributing_agents={},
        aggregated_confidence=0.8,
        account_equity_at_entry=1000.0,
        atr_pct_at_entry=0.02,
        market_regime_at_entry="normal",
        stop_loss_pct=HARD_STOP_LOSS_PCT,
        take_profit_pct=DEFAULT_STRATEGY.take_profit_pct,
        config_version_id=None,
        active_model="claude-fable-5",
        entry_ts="2024-01-02T20:00:00+00:00",
    )
    return eng, trade_id


def test_stop_fires_on_breach_day_not_before() -> None:
    # 5 benign days (low 95, above the 82 stop), then day 6 gaps down and the low hits 80.
    stop = 100.0 * (1 - HARD_STOP_LOSS_PCT)  # 82
    benign = (98.0, 99.0, 95.0, 97.0)  # low 95 > 82
    crash = (90.0, 92.0, 80.0, 85.0)  # low 80 < 82 -> stop
    rows = [benign] * 6 + [crash]
    store = make_store({"AAA": rows, "SPY": flat(400, 7)})
    eng, _ = _engine_with_open_position(store)
    dates = store.bars["AAA"].index

    # Days 0..5: the future crash is invisible; the position must stay open.
    for i in range(6):
        now = datetime(dates[i].year, dates[i].month, dates[i].day, 20, tzinfo=UTC)
        eng._process_exits(dates[i], now, "normal")
        assert eng.db.open_position_count() == 1, f"stopped early on day {i} (look-ahead!)"

    # Day 6: the low breaches the stop -> exit exactly here.
    now = datetime(dates[6].year, dates[6].month, dates[6].day, 20, tzinfo=UTC)
    eng._process_exits(dates[6], now, "normal")
    assert eng.db.open_position_count() == 0
    closed = eng.db.closed_trades()
    assert closed[0]["exit_reason"] == REASON_STOP_LOSS
    # Fill is at the stop, reduced by slippage (open 90 >= stop 82, so no gap-through).
    assert closed[0]["exit_price"] < stop
    assert abs(closed[0]["exit_price"] - stop * (1 - 0.0005)) < 0.01


# === realistic costs: slippage + T+1 settlement ============================


def test_entry_price_includes_buy_slippage() -> None:
    store = make_store({"AAA": flat(100.0, 30), "SPY": flat(400, 30)})
    cfg = BacktestConfig(
        universe=["AAA"], start="x", end="y", slippage_per_side=0.001, db_path=":memory:"
    )
    eng = BacktestEngine(cfg, store=store)
    d = store.bars["AAA"].index[10]
    # Buy fill pays up by one slippage step above the close.
    assert eng._entry_price(d, "AAA") == round(100.0 * 1.001, 4)


def test_sale_proceeds_are_unsettled_until_next_day() -> None:
    stop_rows = [(90.0, 92.0, 80.0, 85.0)]  # immediate stop
    store = make_store({"AAA": stop_rows, "SPY": flat(400, 1)}, start="2024-03-01")
    eng, _ = _engine_with_open_position(store)
    d = store.bars["AAA"].index[0]
    now = datetime(d.year, d.month, d.day, 20, tzinfo=UTC)
    eng._process_exits(d, now, "normal")
    # Proceeds sit in pending_sales (T+1); not deployable today.
    assert eng.portfolio.unsettled_total() > 0
    assert eng.portfolio.settled_available(now) == eng.portfolio.settled_cash


# === determinism ===========================================================


def test_engine_offline_is_deterministic() -> None:
    store = make_store({"AAA": flat(100.0, 40), "BBB": flat(50.0, 40), "SPY": flat(400, 40)})
    cfg = BacktestConfig(universe=["AAA", "BBB"], start="x", end="y", db_path=":memory:")
    a = BacktestEngine(cfg, store=store).run()
    b = BacktestEngine(cfg, store=store).run()
    ea = [r["total_equity"] for r in a.equity_series()]
    eb = [r["total_equity"] for r in b.equity_series()]
    assert ea == eb


# === efficiency fix 1: thesis on/off must not change which trades close =====


def _closes_with_thesis(thesis: bool) -> list[tuple[str, str, float]]:
    rows = [(98.0, 99.0, 95.0, 97.0)] * 3 + [(90.0, 92.0, 80.0, 85.0)]  # benign then a stop
    store = make_store({"AAA": rows, "SPY": flat(400, 4)})
    cfg = BacktestConfig(
        universe=["AAA"],
        start="x",
        end="y",
        starting_capital=1000.0,
        db_path=":memory:",
        thesis_enabled=thesis,
    )
    eng = BacktestEngine(cfg, store=store)
    eng.portfolio.on_buy(100.0)
    eng.db.insert_trade_entry(
        symbol="AAA",
        entry_price=100.0,
        position_size_usd=100.0,
        shares=1.0,
        contributing_agents={},
        aggregated_confidence=0.8,
        account_equity_at_entry=1000.0,
        atr_pct_at_entry=0.02,
        market_regime_at_entry="normal",
        stop_loss_pct=HARD_STOP_LOSS_PCT,
        take_profit_pct=DEFAULT_STRATEGY.take_profit_pct,
        config_version_id=None,
        active_model="claude-fable-5",
        entry_ts="2024-01-02T20:00:00+00:00",
    )
    for d in store.bars["AAA"].index:
        eng._process_exits(d, datetime(d.year, d.month, d.day, 20, tzinfo=UTC), "normal")
    return [
        (str(t["symbol"]), str(t["exit_reason"]), round(float(t["realized_pnl"]), 4))
        for t in eng.db.closed_trades()
    ]


def test_thesis_toggle_does_not_change_closes() -> None:
    on = _closes_with_thesis(True)
    off = _closes_with_thesis(False)
    assert on == off
    assert len(off) == 1 and off[0][1] == "stop_loss"  # pure-code stop still closed it
