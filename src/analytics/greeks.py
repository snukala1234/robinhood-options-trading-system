"""Greeks with explicit source labels (spec 5.3): broker-supplied vs. calculated.

Units, defined once and used everywhere:

- ``delta``: per-share price change per $1 underlying move (calls 0..1, puts -1..0).
- ``gamma``: delta change per $1 underlying move.
- ``theta_daily``: per-share price change per **calendar day** (year/365).
- ``vega``: per-share price change per **1 percentage point** of implied volatility.
- ``rho``: per-share price change per 1 percentage point of the risk-free rate.

Per-contract dollar values are these numbers multiplied by the contract multiplier;
that multiplication is done in Decimal by callers (e.g. portfolio_exposure).

Black-Scholes values are **model estimates, not money**: the transcendental math runs
in IEEE floats internally, and results are quantized to six decimal places on the way
out, labeled ``CALCULATED`` with the full input assumptions recorded. Broker-supplied
Greeks are labeled ``BROKER`` and are never silently mixed with calculated ones.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from src.domain.instruments import OptionType
from src.domain.values import DomainValidationError, require_money, require_positive_money

_SIX_DP = Decimal("0.000001")


class GreekSource(StrEnum):
    BROKER = "broker"
    CALCULATED = "calculated"


@dataclass(frozen=True)
class GreekSet:
    """One contract's Greeks with provenance. ``assumptions`` required if calculated."""

    delta: Decimal
    gamma: Decimal
    theta_daily: Decimal
    vega: Decimal
    source: GreekSource
    rho: Decimal | None = None
    assumptions: tuple[tuple[str, str], ...] | None = None

    def __post_init__(self) -> None:
        for name in ("delta", "gamma", "theta_daily", "vega"):
            require_money(name, getattr(self, name))
        if self.rho is not None:
            require_money("rho", self.rho)
        if not isinstance(self.source, GreekSource):
            raise DomainValidationError("source must be a GreekSource")
        if self.source is GreekSource.CALCULATED and not self.assumptions:
            raise DomainValidationError("calculated Greeks must record their input assumptions")
        if not (Decimal("-1") <= self.delta <= Decimal("1")):
            raise DomainValidationError(f"delta out of [-1, 1]: {self.delta}")
        if self.gamma < 0:
            raise DomainValidationError(f"gamma must be >= 0: {self.gamma}")


def _quantize(value: float) -> Decimal:
    return Decimal(repr(value)).quantize(_SIX_DP)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def black_scholes_greeks(
    *,
    spot: Decimal,
    strike: Decimal,
    dte: int,
    iv: Decimal,
    risk_free_rate: Decimal,
    option_type: OptionType,
) -> GreekSet:
    """European Black-Scholes Greeks (no dividends), labeled CALCULATED.

    ``dte`` is calendar days to expiration (t = dte/365 years). Rejects expired or
    degenerate inputs instead of extrapolating: dte >= 1, iv in (0, 10),
    rate in (-0.5, 0.5), spot/strike > 0.
    """
    require_positive_money("spot", spot)
    require_positive_money("strike", strike)
    if isinstance(dte, bool) or not isinstance(dte, int) or dte < 1:
        raise DomainValidationError(f"dte must be an int >= 1, got {dte!r}")
    require_positive_money("iv", iv)
    if iv >= 10:
        raise DomainValidationError(f"iv {iv} is not a plausible volatility fraction")
    require_money("risk_free_rate", risk_free_rate)
    if not (Decimal("-0.5") < risk_free_rate < Decimal("0.5")):
        raise DomainValidationError(f"risk_free_rate {risk_free_rate} out of (-0.5, 0.5)")
    if not isinstance(option_type, OptionType):
        raise DomainValidationError("option_type must be an OptionType")

    s, k = float(spot), float(strike)
    t = dte / 365.0
    sigma, r = float(iv), float(risk_free_rate)
    sqrt_t = math.sqrt(t)
    d1 = (math.log(s / k) + (r + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    pdf_d1 = _norm_pdf(d1)
    discount = math.exp(-r * t)

    if option_type is OptionType.CALL:
        delta = _norm_cdf(d1)
        theta_year = -s * pdf_d1 * sigma / (2.0 * sqrt_t) - r * k * discount * _norm_cdf(d2)
        rho = k * t * discount * _norm_cdf(d2) / 100.0
    else:
        delta = _norm_cdf(d1) - 1.0
        theta_year = -s * pdf_d1 * sigma / (2.0 * sqrt_t) + r * k * discount * _norm_cdf(-d2)
        rho = -k * t * discount * _norm_cdf(-d2) / 100.0

    gamma = pdf_d1 / (s * sigma * sqrt_t)
    vega_per_pct = s * pdf_d1 * sqrt_t / 100.0

    return GreekSet(
        delta=_quantize(delta),
        gamma=_quantize(gamma),
        theta_daily=_quantize(theta_year / 365.0),
        vega=_quantize(vega_per_pct),
        rho=_quantize(rho),
        source=GreekSource.CALCULATED,
        assumptions=(
            ("model", "black_scholes_european_no_dividends"),
            ("spot", str(spot)),
            ("strike", str(strike)),
            ("dte", str(dte)),
            ("iv", str(iv)),
            ("risk_free_rate", str(risk_free_rate)),
            ("option_type", option_type.value),
            ("day_count", "365"),
        ),
    )
