"""Section 13.3 metrics — pure Decimal, computed only over real samples.

``compute_metrics`` refuses an empty sample: there is no such thing as a
metric over nothing. Ratios that would divide by zero (profit factor with no
losers, theta return with zero theta) are ``None``, never fabricated.
Everything serializes to JSON-safe strings for ``calibration_results``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from src.domain.values import DomainValidationError
from src.learning.records import FillAttempt, TradeRecord

D = Decimal
_SIX = D("0.000001")
_TWO = D("0.01")


@dataclass(frozen=True)
class BucketMetrics:
    sample_size: int
    win_rate: Decimal
    avg_win: Decimal | None
    avg_loss: Decimal | None  # magnitude of the average losing P&L
    expectancy_after_costs: Decimal
    profit_factor: Decimal | None  # gross wins / gross losses
    avg_mae: Decimal
    avg_mfe: Decimal
    avg_slippage_vs_mid: Decimal
    fill_rate: Decimal | None
    avg_seconds_to_fill: Decimal | None
    return_on_max_risk: Decimal  # mean of pnl / max_risk
    return_per_unit_theta: Decimal | None  # mean of pnl / |theta $ daily|
    return_per_delta_dollar: Decimal | None  # mean of pnl / |delta $|
    return_per_gamma_dollar: Decimal | None  # mean of pnl / |gamma $|
    brier_score: Decimal
    max_drawdown: Decimal  # peak-to-trough of the cumulative P&L curve
    longest_recovery_days: int | None  # longest completed dip-to-recovery span
    in_drawdown_at_window_end: bool

    def to_json(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {
            "sample_size": self.sample_size,
            "win_rate": s(self.win_rate),
            "avg_win": s(self.avg_win),
            "avg_loss": s(self.avg_loss),
            "expectancy_after_costs": s(self.expectancy_after_costs),
            "profit_factor": s(self.profit_factor),
            "avg_mae": s(self.avg_mae),
            "avg_mfe": s(self.avg_mfe),
            "avg_slippage_vs_mid": s(self.avg_slippage_vs_mid),
            "fill_rate": s(self.fill_rate),
            "avg_seconds_to_fill": s(self.avg_seconds_to_fill),
            "return_on_max_risk": s(self.return_on_max_risk),
            "return_per_unit_theta": s(self.return_per_unit_theta),
            "return_per_delta_dollar": s(self.return_per_delta_dollar),
            "return_per_gamma_dollar": s(self.return_per_gamma_dollar),
            "brier_score": s(self.brier_score),
            "max_drawdown": s(self.max_drawdown),
            "longest_recovery_days": self.longest_recovery_days,
            "in_drawdown_at_window_end": self.in_drawdown_at_window_end,
        }


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, D("0")) / len(values)


def _ratio_mean(records: Sequence[TradeRecord], denominator: str) -> Decimal | None:
    """Mean of pnl / |exposure| over records where the exposure is nonzero."""
    ratios = [
        r.pnl_after_costs / abs(getattr(r, denominator))
        for r in records
        if getattr(r, denominator) != 0
    ]
    if not ratios:
        return None
    return _mean(ratios).quantize(_SIX)


def compute_metrics(
    records: Sequence[TradeRecord], fill_attempts: Sequence[FillAttempt] = ()
) -> BucketMetrics:
    """All Section 13.3 metrics over one bucket's records."""
    if not records:
        raise DomainValidationError("metrics require at least one record; never fabricated")

    n = len(records)
    winners = [r for r in records if r.won]
    losers = [r for r in records if not r.won]
    gross_wins = sum((r.pnl_after_costs for r in winners), D("0"))
    gross_losses = sum((-r.pnl_after_costs for r in losers), D("0"))

    filled = [a for a in fill_attempts if a.filled]
    fill_rate = (D(len(filled)) / D(len(fill_attempts))).quantize(_SIX) if fill_attempts else None
    seconds = [a.seconds_to_fill for a in filled if a.seconds_to_fill is not None]
    avg_seconds = _mean(seconds).quantize(_TWO) if seconds else None

    brier = _mean(
        [(r.predicted_win_probability - (D(1) if r.won else D(0))) ** 2 for r in records]
    ).quantize(_SIX)

    max_drawdown, longest_recovery, in_drawdown = _drawdown(records)

    return BucketMetrics(
        sample_size=n,
        win_rate=(D(len(winners)) / D(n)).quantize(_SIX),
        avg_win=_mean([r.pnl_after_costs for r in winners]).quantize(_TWO) if winners else None,
        avg_loss=_mean([-r.pnl_after_costs for r in losers]).quantize(_TWO) if losers else None,
        expectancy_after_costs=_mean([r.pnl_after_costs for r in records]).quantize(_TWO),
        profit_factor=(gross_wins / gross_losses).quantize(_SIX) if gross_losses > 0 else None,
        avg_mae=_mean([r.mae for r in records]).quantize(_TWO),
        avg_mfe=_mean([r.mfe for r in records]).quantize(_TWO),
        avg_slippage_vs_mid=_mean([r.slippage_vs_mid for r in records]).quantize(_TWO),
        fill_rate=fill_rate,
        avg_seconds_to_fill=avg_seconds,
        return_on_max_risk=_mean([r.pnl_after_costs / r.max_risk for r in records]).quantize(_SIX),
        return_per_unit_theta=_ratio_mean(records, "theta_dollars_daily"),
        return_per_delta_dollar=_ratio_mean(records, "delta_dollars"),
        return_per_gamma_dollar=_ratio_mean(records, "gamma_dollars"),
        brier_score=brier,
        max_drawdown=max_drawdown.quantize(_TWO),
        longest_recovery_days=longest_recovery,
        in_drawdown_at_window_end=in_drawdown,
    )


def _drawdown(records: Sequence[TradeRecord]) -> tuple[Decimal, int | None, bool]:
    """Peak-to-trough drawdown of the cumulative after-cost P&L curve (in exit
    order) and the longest completed peak-to-recovery span in days."""
    running = D("0")
    peak = D("0")
    max_dd = D("0")
    dip_started = None
    longest_recovery: int | None = None
    for record in sorted(records, key=lambda r: r.exited_at):
        running += record.pnl_after_costs
        if running >= peak:
            if dip_started is not None:
                days = (record.exited_at - dip_started).days
                longest_recovery = max(longest_recovery or 0, days)
                dip_started = None
            peak = running
        else:
            if dip_started is None:
                dip_started = record.exited_at
            max_dd = max(max_dd, peak - running)
    return max_dd, longest_recovery, dip_started is not None
