"""Backtest performance report: metrics, calibration, and by-year / by-regime breakdowns.

Calibration reuses the *same* bucketing Agent 8 uses (``CalibrationAuditor.compute_buckets``),
so "did the confidence scores match realized hit rates" is answered with the production logic.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

from agents.auditor import CalibrationAuditor
from core.db import Database
from core.stats import mean, stdev

TRADING_DAYS = 252


def _num(value: object) -> float:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


@dataclass
class Breakdown:
    key: str
    trades: int
    wins: int
    win_rate: float
    total_pnl: float


@dataclass
class BacktestReport:
    start: str
    end: str
    trading_days: int
    starting_equity: float
    ending_equity: float
    total_return_pct: float
    total_pnl: float
    max_drawdown_pct: float
    sharpe_annualized: float
    trades: int
    open_at_end: int
    wins: int
    losses: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    exposure_pct: float
    calibration: list[dict[str, object]] = field(default_factory=list)
    by_year: list[Breakdown] = field(default_factory=list)
    by_regime: list[Breakdown] = field(default_factory=list)


def _max_drawdown(equity: list[float]) -> float:
    peak = -math.inf
    mdd = 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


def _breakdowns(
    trades: list[dict[str, object]], key_fn: Callable[[dict[str, object]], str]
) -> list[Breakdown]:
    groups: dict[str, list[float]] = {}
    for t in trades:
        k = key_fn(t)
        groups.setdefault(k, []).append(_num(t.get("realized_pnl")))
    out = []
    for k in sorted(groups):
        pnls = groups[k]
        wins = sum(1 for p in pnls if p > 0)
        out.append(Breakdown(
            key=k, trades=len(pnls), wins=wins,
            win_rate=round(wins / len(pnls), 4) if pnls else 0.0,
            total_pnl=round(sum(pnls), 2),
        ))
    return out


def compute_report(db: Database, start: str, end: str, starting_capital: float) -> BacktestReport:
    series = db.equity_series(is_paper=True)
    equity = [r["total_equity"] for r in series]
    starting = starting_capital
    ending = equity[-1] if equity else starting_capital

    daily_returns = [
        (equity[i] - equity[i - 1]) / equity[i - 1]
        for i in range(1, len(equity)) if equity[i - 1]
    ]
    sd = stdev(daily_returns)
    sharpe = (mean(daily_returns) / sd * math.sqrt(TRADING_DAYS)) if sd else 0.0

    closed = db.closed_trades()
    pnls = [_num(t.get("realized_pnl")) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)

    # Exposure: fraction of simulated days with at least one open position.
    days_with_positions = sum(1 for r in series if (r["open_position_count"] or 0) > 0)

    auditor = CalibrationAuditor(db)
    calibration = [
        {
            "source_agent": b.source_agent, "band": b.band, "n": b.sample_size,
            "observed_hit_rate": b.observed_hit_rate, "expected_hit_rate": b.expected_hit_rate,
            "z_score": b.z_score,
        }
        for b in auditor.compute_buckets(persist=False)
    ]

    return BacktestReport(
        start=start, end=end, trading_days=len(series), starting_equity=round(starting, 2),
        ending_equity=round(ending, 2),
        total_return_pct=round((ending - starting) / starting, 4) if starting else 0.0,
        total_pnl=round(ending - starting, 2),
        max_drawdown_pct=round(_max_drawdown(equity), 4),
        sharpe_annualized=round(sharpe, 3),
        trades=len(closed), open_at_end=db.open_position_count(),
        wins=len(wins), losses=len(losses),
        win_rate=round(len(wins) / len(closed), 4) if closed else 0.0,
        avg_win=round(mean(wins), 2), avg_loss=round(mean(losses), 2),
        profit_factor=round(gross_win / gross_loss, 3) if gross_loss else 0.0,
        exposure_pct=round(days_with_positions / len(series), 4) if series else 0.0,
        calibration=calibration,
        by_year=_breakdowns(closed, lambda t: str(t.get("exit_ts") or "")[:4] or "?"),
        by_regime=_breakdowns(closed, lambda t: str(t.get("market_regime_at_entry") or "?")),
    )


def format_report(r: BacktestReport, benchmark_return: float | None = None) -> str:
    lines: list[str] = []
    lines.append("=" * 64)
    lines.append(f"BACKTEST REPORT  {r.start} -> {r.end}   ({r.trading_days} trading days)")
    lines.append("=" * 64)
    lines.append("PERFORMANCE")
    lines.append(f"  starting equity     ${r.starting_equity:,.2f}")
    lines.append(f"  ending equity       ${r.ending_equity:,.2f}")
    lines.append(f"  total return        {r.total_return_pct*100:+.2f}%   (${r.total_pnl:+,.2f})")
    if benchmark_return is not None:
        diff = r.total_return_pct - benchmark_return
        lines.append(f"  SPY buy & hold      {benchmark_return*100:+.2f}%   "
                     f"(strategy {diff*100:+.2f}% vs SPY)")
    lines.append(f"  max drawdown        {r.max_drawdown_pct*100:.2f}%")
    lines.append(f"  Sharpe (annualized) {r.sharpe_annualized}")
    lines.append(f"  market exposure     {r.exposure_pct*100:.1f}% of days")
    lines.append("")
    lines.append("TRADES")
    lines.append(f"  closed trades       {r.trades}   (open at end: {r.open_at_end})")
    lines.append(f"  win rate            {r.win_rate*100:.1f}%   ({r.wins}W / {r.losses}L)")
    lines.append(f"  avg win / avg loss  ${r.avg_win:+,.2f} / ${r.avg_loss:+,.2f}")
    lines.append(f"  profit factor       {r.profit_factor}")
    lines.append("")
    lines.append("CALIBRATION  (confidence band vs realized hit rate -- Agent 8 bucketing)")
    lines.append(f"  {'agent':22s} {'band':10s} {'n':>4s} {'obs':>6s} {'exp':>6s} {'z':>7s}")
    for c in r.calibration:
        lines.append(f"  {str(c['source_agent']):22s} {str(c['band']):10s} {int(_num(c['n'])):>4} "
                     f"{_num(c['observed_hit_rate']):>6.2f} {_num(c['expected_hit_rate']):>6.2f} "
                     f"{_num(c['z_score']):>7.2f}")
    lines.append("")
    lines.append("BY YEAR")
    for b in r.by_year:
        lines.append(f"  {b.key}   trades {b.trades:>4}  win {b.win_rate*100:>5.1f}%  "
                     f"pnl ${b.total_pnl:+,.2f}")
    lines.append("")
    lines.append("BY REGIME (at entry)")
    for b in r.by_regime:
        lines.append(f"  {b.key:10s} trades {b.trades:>4}  win {b.win_rate*100:>5.1f}%  "
                     f"pnl ${b.total_pnl:+,.2f}")
    return "\n".join(lines)
