"""Pure statistical primitives for Agent 8 (calibration significance + shadow comparison).

All functions are deterministic and dependency-light (stdlib ``statistics``/``math``). The
z-test gates calibration adaptation (Section 3.2); the Sharpe + Welch comparison gates
shadow-config promotion (Section 3.5).
"""

from __future__ import annotations

import math
import statistics

# Two-sided 95% critical value used throughout (Section 3.2 uses |z| >= 1.96).
Z_CRITICAL_95 = 1.96


def proportion_z_score(observed_rate: float, expected_rate: float, n: int) -> float:
    """z-score for an observed hit rate vs. an expected rate under a binomial model."""
    if n <= 0:
        return 0.0
    if expected_rate <= 0.0 or expected_rate >= 1.0:
        return 0.0
    se = math.sqrt(expected_rate * (1.0 - expected_rate) / n)
    if se == 0.0:
        return 0.0
    return (observed_rate - expected_rate) / se


def is_significant(z_score: float, critical: float = Z_CRITICAL_95) -> bool:
    """True if |z| meets the significance threshold (default ~95%)."""
    return abs(z_score) >= critical


def mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def stdev(values: list[float]) -> float:
    """Sample standard deviation; 0.0 for fewer than two points."""
    return statistics.stdev(values) if len(values) >= 2 else 0.0


def sharpe_ratio(returns: list[float]) -> float:
    """Per-trade Sharpe (mean/stdev of returns). 0.0 if undefined (n<2 or zero variance)."""
    if len(returns) < 2:
        return 0.0
    sd = stdev(returns)
    if sd == 0.0:
        return 0.0
    return mean(returns) / sd


def welch_t(a: list[float], b: list[float]) -> float:
    """Welch's t statistic for the difference of means of two samples."""
    if len(a) < 2 or len(b) < 2:
        return 0.0
    va = statistics.variance(a)
    vb = statistics.variance(b)
    denom = math.sqrt(va / len(a) + vb / len(b))
    if denom == 0.0:
        return 0.0
    return (mean(a) - mean(b)) / denom


def difference_is_significant(a: list[float], b: list[float]) -> bool:
    """True if samples ``a`` and ``b`` differ significantly (normal approx, n>=30 assumed)."""
    return is_significant(welch_t(a, b))
