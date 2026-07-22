"""Liquidity and execution-cost estimates (spec 5.6) — pure Decimal.

Cost model, stated explicitly: entering at the midpoint is optimistic; the
conservative estimate is paying the half-spread per share on entry and again on
exit. ``est_entry_slippage`` and ``est_round_trip_cost`` are total dollars for
``quantity`` contracts at the given multiplier.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.config.risk_policy import (
    MAX_BID_ASK_SPREAD_PCT,
    MIN_CONTRACT_PRICE,
    MIN_OPTION_DAILY_VOLUME,
    MIN_OPTION_OPEN_INTEREST,
)
from src.data.option_chains import ContractQuote
from src.domain.values import DomainValidationError, require_positive_int

_SIX_DP = Decimal("0.000001")


@dataclass(frozen=True)
class LiquidityAssessment:
    """Spread, depth proxies, cost estimates, and pass/fail against policy floors."""

    spread_abs: Decimal
    spread_pct: Decimal
    midpoint: Decimal
    open_interest: int
    volume: int
    est_entry_slippage: Decimal
    est_round_trip_cost: Decimal
    passes: bool
    failures: tuple[str, ...]


def assess(quote: ContractQuote, quantity: int) -> LiquidityAssessment:
    """Assess one contract's liquidity for a given contract quantity."""
    require_positive_int("quantity", quantity)
    if quote.midpoint <= 0:
        raise DomainValidationError("midpoint must be > 0 to assess liquidity")

    spread_abs = quote.ask - quote.bid
    spread_pct = (spread_abs / quote.midpoint).quantize(_SIX_DP)
    scale = Decimal(quote.contract.multiplier) * Decimal(quantity)
    half_spread = spread_abs / 2
    est_entry_slippage = half_spread * scale
    est_round_trip_cost = spread_abs * scale

    failures: list[str] = []
    if quote.open_interest < MIN_OPTION_OPEN_INTEREST:
        failures.append(f"open_interest {quote.open_interest} < {MIN_OPTION_OPEN_INTEREST}")
    if quote.volume < MIN_OPTION_DAILY_VOLUME:
        failures.append(f"volume {quote.volume} < {MIN_OPTION_DAILY_VOLUME}")
    if spread_pct > Decimal(str(MAX_BID_ASK_SPREAD_PCT)):
        failures.append(f"spread_pct {spread_pct} > {MAX_BID_ASK_SPREAD_PCT}")
    if quote.midpoint < Decimal(str(MIN_CONTRACT_PRICE)):
        failures.append(f"midpoint {quote.midpoint} < {MIN_CONTRACT_PRICE}")

    return LiquidityAssessment(
        spread_abs=spread_abs,
        spread_pct=spread_pct,
        midpoint=quote.midpoint,
        open_interest=quote.open_interest,
        volume=quote.volume,
        est_entry_slippage=est_entry_slippage,
        est_round_trip_cost=est_round_trip_cost,
        passes=not failures,
        failures=tuple(failures),
    )
