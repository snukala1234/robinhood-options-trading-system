"""Guardrail tests (Section 0 + Section 1.1).

Each test *attempts* to violate a hard limit and asserts the system structurally blocks
it. Per the build directive, a failure here means the FEATURE is wrong, never the guardrail:
these tests must never be weakened, skipped, or xfail'd to make the build pass.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from config import guardrails as G
from config.strategy import DEFAULT_STRATEGY
from core.records import AggregatedSignal, Position
from risk.sizing import (
    Account,
    Sale,
    assert_purchase_is_covered,
    calculate_position_size,
    check_portfolio_halt,
    check_stop_loss,
    check_stop_loss_cash_account,
    passes_confidence_gate,
    settled_cash,
)


def make_signal(
    symbol: str = "AAPL",
    price: float = 100.0,
    atr_14: float = 2.0,
    confidence: float = 0.80,
) -> AggregatedSignal:
    return AggregatedSignal(
        symbol=symbol,
        direction="long",
        magnitude=0.7,
        calibrated_confidence=confidence,
        current_price=price,
        atr_14=atr_14,
        market_regime="normal",
        reasoning="test",
        active_model="claude-fable-5",
    )


def make_position(symbol: str = "AAPL", entry_price: float = 100.0) -> Position:
    return Position(
        trade_id="t-" + symbol,
        symbol=symbol,
        entry_price=entry_price,
        shares=1.0,
        position_size_usd=entry_price,
        entry_ts="2026-07-05T00:00:00+00:00",
        stop_loss_pct=G.HARD_STOP_LOSS_PCT,
        take_profit_pct=DEFAULT_STRATEGY.take_profit_pct,
    )


NOW = datetime(2026, 7, 5, 15, 0, tzinfo=UTC)


# === Section 0 constant integrity =========================================


def test_section0_constants_have_expected_values() -> None:
    # Drift detection: these are the spec's hard limits and must not silently change.
    assert G.MAX_POSITION_PCT_OF_EQUITY == 0.40
    assert G.MAX_CONCURRENT_POSITIONS == 3
    assert G.MAX_DAILY_LOSS_PCT == 0.10
    assert G.MAX_DRAWDOWN_HALT_PCT == 0.25
    assert G.MIN_SIGNAL_CONFIDENCE_TO_TRADE == 0.65
    assert G.ORDER_APPROVAL_MODE == "manual"
    assert G.PAPER_TRADING is True
    assert G.ENFORCE_SETTLED_CASH_ONLY is True
    assert G.MAX_DAY_TRADES_PER_5_SESSIONS is None
    assert G.MIN_SAMPLE_SIZE_FOR_ADAPTATION == 30


def test_hard_stop_within_documented_band() -> None:
    assert G.hard_stop_within_band()
    assert 0.15 <= G.HARD_STOP_LOSS_PCT <= 0.20


# === MAX_POSITION_PCT_OF_EQUITY ============================================


def test_size_never_exceeds_max_position_pct_of_equity() -> None:
    # Attempt: enormous settled cash so only the equity cap can bind.
    equity = 500.0
    size = calculate_position_size(
        equity,
        settled_cash_amount=10_000.0,
        signal=make_signal(confidence=1.0, atr_14=0.5),
        open_positions=[],
    )
    assert size <= equity * G.MAX_POSITION_PCT_OF_EQUITY + 1e-9


# === settled-cash cap on sizing (Section 1.1) ==============================


def test_size_never_exceeds_settled_cash() -> None:
    # Attempt: high equity cap but only $30 settled — size must not exceed settled cash.
    size = calculate_position_size(
        account_equity=1_000.0,
        settled_cash_amount=30.0,
        signal=make_signal(confidence=1.0, atr_14=0.5),
        open_positions=[],
    )
    assert size <= 30.0 + 1e-9


def test_zero_settled_cash_blocks_sizing() -> None:
    size = calculate_position_size(
        account_equity=1_000.0, settled_cash_amount=0.0, signal=make_signal(), open_positions=[]
    )
    assert size == 0.0


# === MAX_CONCURRENT_POSITIONS ==============================================


def test_max_concurrent_positions_blocks_new_entry() -> None:
    open_positions = [make_position(s) for s in ("AAPL", "JPM", "NFLX")]
    assert len(open_positions) == G.MAX_CONCURRENT_POSITIONS
    size = calculate_position_size(
        account_equity=1_000.0,
        settled_cash_amount=500.0,
        signal=make_signal(symbol="TSLA"),
        open_positions=open_positions,
    )
    assert size == 0.0


# === MIN_SIGNAL_CONFIDENCE_TO_TRADE ========================================


def test_confidence_gate_blocks_subthreshold_signal() -> None:
    assert passes_confidence_gate(0.65) is True
    assert passes_confidence_gate(0.649) is False
    assert passes_confidence_gate(0.50) is False


# === HARD_STOP_LOSS_PCT (forced, unconditional) ============================


def test_hard_stop_loss_fires_at_threshold_and_is_forced() -> None:
    pos = make_position(entry_price=100.0)
    at_threshold = 100.0 * (1 - G.HARD_STOP_LOSS_PCT)
    signal = check_stop_loss(pos, current_price=at_threshold)
    assert signal is not None and signal.forced is True and signal.reason == "stop_loss"


def test_hard_stop_loss_fires_beyond_threshold() -> None:
    pos = make_position(entry_price=100.0)
    signal = check_stop_loss(pos, current_price=50.0)
    assert signal is not None and signal.forced is True


def test_hard_stop_loss_does_not_fire_above_threshold() -> None:
    pos = make_position(entry_price=100.0)
    just_above = 100.0 * (1 - G.HARD_STOP_LOSS_PCT) + 0.5
    assert check_stop_loss(pos, current_price=just_above) is None


def test_cash_account_stop_is_unconditional_same_as_hard_stop() -> None:
    # Section 1.1: the sell-side stop can never create a GFV, so it is identical.
    pos = make_position(entry_price=100.0)
    price = 100.0 * (1 - G.HARD_STOP_LOSS_PCT)
    assert check_stop_loss_cash_account(pos, price) == check_stop_loss(pos, price)


# === MAX_DAILY_LOSS_PCT / MAX_DRAWDOWN_HALT_PCT ============================


def test_daily_loss_limit_halts_new_entries() -> None:
    # Attempt: down exactly 10% on the day.
    halt = check_portfolio_halt(
        account_equity=90.0, high_water_mark=100.0, daily_start_equity=100.0
    )
    assert halt is not None
    assert halt.action == "halt_new_entries" and halt.resumes_next_session is True


def test_drawdown_halt_stops_all_trading_and_requires_manual_resume() -> None:
    # Attempt: 25% below high-water mark.
    halt = check_portfolio_halt(account_equity=75.0, high_water_mark=100.0, daily_start_equity=76.0)
    assert halt is not None
    assert halt.action == "halt_all_trading" and halt.requires_manual_resume is True


def test_no_halt_within_limits() -> None:
    assert (
        check_portfolio_halt(account_equity=98.0, high_water_mark=100.0, daily_start_equity=100.0)
        is None
    )


# === Section 1.1 settled-cash / GFV invariant ==============================


def test_settled_cash_excludes_unsettled_proceeds() -> None:
    account = Account(
        cleared_deposits=100.0,
        recent_sales=[
            Sale(proceeds=50.0, settlement_date=(NOW - timedelta(days=1)).date()),  # settled
            Sale(proceeds=40.0, settlement_date=(NOW + timedelta(days=1)).date()),  # unsettled
        ],
    )
    # Only cleared deposits + the settled sale count.
    assert settled_cash(account, NOW) == pytest.approx(150.0)


def test_purchase_with_unsettled_funds_is_blocked() -> None:
    # Attempt the exact GFV: buy using proceeds that have not settled yet.
    account = Account(
        cleared_deposits=20.0,
        recent_sales=[Sale(proceeds=100.0, settlement_date=(NOW + timedelta(days=1)).date())],
    )
    # $120 order but only $20 is settled -> must be blocked.
    decision = assert_purchase_is_covered(order_cost=120.0, account=account, now=NOW)
    assert decision.allowed is False
    assert decision.reason == "would_use_unsettled_funds"


def test_purchase_fully_covered_by_settled_cash_is_allowed() -> None:
    account = Account(cleared_deposits=200.0, recent_sales=[])
    assert assert_purchase_is_covered(order_cost=150.0, account=account, now=NOW).allowed is True


def test_sizing_cannot_fund_a_purchase_beyond_settled_cash_end_to_end() -> None:
    # The structural guarantee: whatever sizing returns is always coverable by settled cash,
    # so assert_purchase_is_covered can never block a size-derived order.
    settled = 40.0
    account = Account(cleared_deposits=settled, recent_sales=[])
    size = calculate_position_size(
        account_equity=1_000.0,
        settled_cash_amount=settled,
        signal=make_signal(confidence=1.0, atr_14=0.5),
        open_positions=[],
    )
    assert assert_purchase_is_covered(order_cost=size, account=account, now=NOW).allowed is True
