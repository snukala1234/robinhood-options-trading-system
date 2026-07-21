"""Pure-code risk engine — Section 1 and Section 1.1, implemented exactly.

NOT an LLM prompt anywhere. This is the last line of defence on capital-at-risk
decisions, so it is deterministic code. With :data:`config.strategy.DEFAULT_STRATEGY`
the numbers reproduce the Section 1 pseudocode literally; a shadow ``StrategyParams`` may
vary the *tunable* scalars (never the Section 0 guardrails).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from config.guardrails import (
    HARD_STOP_LOSS_PCT,
    MAX_CONCURRENT_POSITIONS,
    MAX_DAILY_LOSS_PCT,
    MAX_DRAWDOWN_HALT_PCT,
    MAX_POSITION_PCT_OF_EQUITY,
    MIN_SIGNAL_CONFIDENCE_TO_TRADE,
)
from config.strategy import DEFAULT_STRATEGY, StrategyParams
from core.records import AggregatedSignal, Position

# --- decision result types --------------------------------------------------


@dataclass(frozen=True)
class ExitSignal:
    """A forced/soft exit instruction. ``forced`` stops are not overridable by any agent."""

    reason: str
    forced: bool


@dataclass(frozen=True)
class HaltDecision:
    """A portfolio-level halt. Either 'halt_all_trading' or 'halt_new_entries'."""

    action: str  # "halt_all_trading" | "halt_new_entries"
    reason: str
    requires_manual_resume: bool = False
    resumes_next_session: bool = False


@dataclass(frozen=True)
class PurchaseDecision:
    """Result of the settled-cash coverage check (Section 1.1)."""

    allowed: bool
    reason: str | None = None


ALLOW = PurchaseDecision(allowed=True)


def BLOCK_ENTRY(reason: str) -> PurchaseDecision:  # noqa: N802 - mirrors spec pseudocode
    return PurchaseDecision(allowed=False, reason=reason)


# --- cash-account model (Section 1.1) --------------------------------------


@dataclass(frozen=True)
class Sale:
    """A completed sale whose proceeds settle on ``settlement_date`` (T+1)."""

    proceeds: float
    settlement_date: date


@dataclass
class Account:
    """Minimal cash-account view used by the settled-cash guard."""

    cleared_deposits: float
    recent_sales: list[Sale] = field(default_factory=list)


# --- helpers ----------------------------------------------------------------


def clamp(value: float, lo: float, hi: float) -> float:
    """Clamp ``value`` into [lo, hi] (matches the spec's ``clamp(x, min, max)``)."""
    return max(lo, min(hi, value))


# Coarse sector map for the Phase-1 universe; unknown symbols get a unique pseudo-sector
# (their own ticker) so two unknown names are treated as uncorrelated, not falsely paired.
SECTOR_MAP: dict[str, str] = {
    "AAPL": "tech_hardware",
    "MSFT": "tech_software",
    "NVDA": "semis",
    "AMD": "semis",
    "GOOGL": "internet",
    "META": "internet",
    "AMZN": "consumer_internet",
    "NFLX": "media",
    "TSLA": "autos",
    "JPM": "financials",
    "SPY": "index",
}


def sector_correlation(symbol_a: str, symbol_b: str) -> float:
    """Rough correlation proxy in [0, 1] used by Section 1's step-5 de-risking.

    Same symbol -> 1.0; same sector -> 0.7 (> 0.6 triggers the size penalty);
    otherwise -> 0.2 (uncorrelated).
    """
    if symbol_a == symbol_b:
        return 1.0
    sector_a = SECTOR_MAP.get(symbol_a, symbol_a)
    sector_b = SECTOR_MAP.get(symbol_b, symbol_b)
    return 0.7 if sector_a == sector_b else 0.2


def passes_confidence_gate(calibrated_confidence: float) -> bool:
    """Section 0 gate: only calibrated confidence >= MIN_SIGNAL_CONFIDENCE_TO_TRADE trades."""
    return calibrated_confidence >= MIN_SIGNAL_CONFIDENCE_TO_TRADE


# --- Section 1: position sizing --------------------------------------------


def calculate_position_size(
    account_equity: float,
    settled_cash_amount: float,
    signal: AggregatedSignal,
    open_positions: list[Position],
    params: StrategyParams = DEFAULT_STRATEGY,
) -> float:
    """Return a target dollar amount to allocate to a new position (Section 1).

    Fractional shares assumed. The result is capped at BOTH the settled-cash amount and
    ``account_equity * MAX_POSITION_PCT_OF_EQUITY`` — this is what makes a good-faith
    violation structurally impossible (every buy is fully paid with settled funds).
    """
    # 1. Respect settled-cash constraint (T+1 settlement, avoid good-faith violations).
    available_capital = min(settled_cash_amount, account_equity * MAX_POSITION_PCT_OF_EQUITY)
    if available_capital <= 0:
        return 0.0

    # 2. Don't exceed max concurrent positions.
    if len(open_positions) >= MAX_CONCURRENT_POSITIONS:
        return 0.0  # no new entries; must wait for an exit

    # 3. Volatility-adjusted sizing (inverse to recent realized volatility, ATR-based).
    atr_pct = signal.atr_14 / signal.current_price if signal.current_price else 0.0
    if atr_pct <= 0:
        volatility_scalar = params.vol_scalar_max
    else:
        volatility_scalar = clamp(
            params.vol_target / atr_pct, params.vol_scalar_min, params.vol_scalar_max
        )

    # 4. Confidence-adjusted sizing.
    confidence_scalar = clamp(
        (signal.calibrated_confidence - MIN_SIGNAL_CONFIDENCE_TO_TRADE)
        / (1.0 - MIN_SIGNAL_CONFIDENCE_TO_TRADE),
        params.conf_scalar_min,
        params.conf_scalar_max,
    )

    # 5. Sector/correlation check — reduce size if already exposed to correlated names.
    correlation_scalar = 1.0
    for pos in open_positions:
        if sector_correlation(pos.symbol, signal.symbol) > params.correlation_threshold:
            correlation_scalar *= params.correlation_penalty

    raw_size = available_capital * volatility_scalar * confidence_scalar * correlation_scalar
    return round(min(raw_size, available_capital), 2)


# --- Section 1: stop-loss ---------------------------------------------------


def check_stop_loss(position: Position, current_price: float) -> ExitSignal | None:
    """Forced stop-loss (Section 1). Not overridable by any agent."""
    loss_pct = (position.entry_price - current_price) / position.entry_price
    if loss_pct >= HARD_STOP_LOSS_PCT:
        return ExitSignal(reason="stop_loss", forced=True)
    return None


def check_stop_loss_cash_account(position: Position, current_price: float) -> ExitSignal | None:
    """Section 1.1: identical to :func:`check_stop_loss`.

    No settlement guard is needed on the SELL side: because the position was bought with
    settled cash (the Section 1.1 invariant), selling it can never create a GFV, so the
    stop-loss is unconditional.
    """
    return check_stop_loss(position, current_price)


# --- Section 1: portfolio halts --------------------------------------------


def check_portfolio_halt(
    account_equity: float, high_water_mark: float, daily_start_equity: float
) -> HaltDecision | None:
    """Portfolio-level circuit breakers (Section 1)."""
    drawdown = (high_water_mark - account_equity) / high_water_mark if high_water_mark else 0.0
    daily_loss = (
        (daily_start_equity - account_equity) / daily_start_equity if daily_start_equity else 0.0
    )

    if drawdown >= MAX_DRAWDOWN_HALT_PCT:
        return HaltDecision(
            action="halt_all_trading",
            reason="max_drawdown_breached",
            requires_manual_resume=True,
        )
    if daily_loss >= MAX_DAILY_LOSS_PCT:
        return HaltDecision(
            action="halt_new_entries",
            reason="daily_loss_limit",
            resumes_next_session=True,
        )
    return None


# --- Section 1.1: settled-cash / good-faith-violation guard ----------------


def settled_cash(account: Account, now: datetime) -> float:
    """Cleared deposits + proceeds from sales whose T+1 settlement date has passed.

    Unsettled proceeds (sold but not yet settled) are EXCLUDED (Section 1.1).
    """
    return account.cleared_deposits + sum(
        s.proceeds for s in account.recent_sales if s.settlement_date <= now.date()
    )


def assert_purchase_is_covered(
    order_cost: float, account: Account, now: datetime
) -> PurchaseDecision:
    """Defensive backstop (Section 1.1): a purchase must draw only on settled cash.

    Blocking is correct here — a blocked entry is cheap; a good-faith violation is a
    90-day account restriction. ``ENFORCE_SETTLED_CASH_ONLY`` is read live from the
    guardrail module so the invariant cannot be silently disabled elsewhere.
    """
    from config.guardrails import ENFORCE_SETTLED_CASH_ONLY

    if not ENFORCE_SETTLED_CASH_ONLY:
        return ALLOW
    if order_cost > settled_cash(account, now) + 1e-6:
        return BLOCK_ENTRY("would_use_unsettled_funds")  # wait for settlement instead
    return ALLOW
