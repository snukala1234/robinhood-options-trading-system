"""Tests for Agent 4 (portfolio construction) and Agent 5 (risk manager)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agents.portfolio_construction import PortfolioConstruction, TradeProposal
from agents.risk_manager import RiskManager
from config.guardrails import HARD_STOP_LOSS_PCT
from config.strategy import DEFAULT_STRATEGY
from core.db import Database
from core.llm import ModelClient, OfflineProvider
from core.records import LONG, SHORT, AggregatedSignal, Position
from risk.sizing import Account, Sale

NOW = datetime(2026, 7, 5, 15, 0, tzinfo=UTC)


def client(db: Database | None = None) -> ModelClient:
    return ModelClient(provider=OfflineProvider(), db=db)


def signal(direction: str = LONG, conf: float = 0.80, price: float = 100.0) -> AggregatedSignal:
    return AggregatedSignal(
        symbol="AAPL",
        direction=direction,
        magnitude=0.6,
        calibrated_confidence=conf,
        current_price=price,
        atr_14=2.0,
        market_regime="normal",
        reasoning="r",
        active_model="claude-fable-5",
        contributing={"research_technical": {"confidence": conf}},
    )


def settled_account(amount: float) -> Account:
    return Account(cleared_deposits=amount, recent_sales=[])


def position(symbol: str) -> Position:
    return Position(
        f"t-{symbol}",
        symbol,
        100.0,
        1.0,
        100.0,
        "2026-07-05T00:00:00+00:00",
        HARD_STOP_LOSS_PCT,
        DEFAULT_STRATEGY.take_profit_pct,
    )


# === Portfolio Construction (Agent 4) ======================================


def test_construction_builds_viable_long_proposal() -> None:
    pc = PortfolioConstruction(client=client())
    prop = pc.build(signal(), account_equity=500.0, settled_cash_amount=200.0, open_positions=[])
    assert prop.viable is True
    assert prop.direction == LONG
    assert 0 < prop.size_usd <= 200.0
    assert prop.shares > 0
    assert prop.stop_loss_pct == HARD_STOP_LOSS_PCT


def test_construction_rejects_short_on_cash_account() -> None:
    pc = PortfolioConstruction(client=client())
    prop = pc.build(signal(direction=SHORT), 500.0, 200.0, [])
    assert prop.viable is False
    assert prop.reason == "short_not_allowed_cash_account"


def test_construction_rejects_below_confidence() -> None:
    pc = PortfolioConstruction(client=client())
    prop = pc.build(signal(conf=0.60), 500.0, 200.0, [])
    assert prop.viable is False
    assert prop.reason == "below_min_confidence"


def test_construction_rejects_when_concurrency_cap_hit() -> None:
    pc = PortfolioConstruction(client=client())
    open_positions = [position(s) for s in ("MSFT", "JPM", "NFLX")]
    prop = pc.build(signal(), 500.0, 200.0, open_positions)
    assert prop.viable is False
    assert prop.reason == "size_zero_concurrency_or_no_settled_cash"


# === Risk Manager (Agent 5) ================================================


def _viable_proposal(size_usd: float = 40.0, price: float = 100.0) -> TradeProposal:
    return TradeProposal(
        symbol="AAPL",
        direction=LONG,
        entry_price=price,
        size_usd=size_usd,
        shares=size_usd / price,
        stop_loss_pct=HARD_STOP_LOSS_PCT,
        take_profit_pct=DEFAULT_STRATEGY.take_profit_pct,
        aggregated_confidence=0.80,
        atr_pct=0.02,
        market_regime="normal",
        active_model="claude-fable-5",
        decided_under_failover=False,
        viable=True,
        reason="ok",
    )


def test_risk_manager_approves_valid_long() -> None:
    rm = RiskManager(client=client())
    decision = rm.evaluate(
        _viable_proposal(40.0),
        settled_account(100.0),
        NOW,
        account_equity=500.0,
        high_water_mark=500.0,
        daily_start_equity=500.0,
        open_positions=[],
    )
    assert decision.approved is True
    assert decision.reason == "approved"
    assert decision.narrative_flag  # narrative present but non-authoritative


def test_risk_manager_blocks_on_drawdown_halt() -> None:
    rm = RiskManager(client=client())
    # 25% below high-water mark -> halt_all_trading.
    decision = rm.evaluate(
        _viable_proposal(),
        settled_account(1000.0),
        NOW,
        account_equity=75.0,
        high_water_mark=100.0,
        daily_start_equity=76.0,
        open_positions=[],
    )
    assert decision.approved is False
    assert decision.halt is not None and decision.halt.action == "halt_all_trading"


def test_risk_manager_blocks_on_daily_loss_halt() -> None:
    rm = RiskManager(client=client())
    decision = rm.evaluate(
        _viable_proposal(),
        settled_account(1000.0),
        NOW,
        account_equity=90.0,
        high_water_mark=100.0,
        daily_start_equity=100.0,
        open_positions=[],
    )
    assert decision.approved is False
    assert decision.halt is not None and decision.halt.action == "halt_new_entries"


def test_risk_manager_blocks_unsettled_funds_backstop() -> None:
    # Defensive backstop: even a manually oversized proposal (size > settled cash) is blocked,
    # proving the GFV guard holds independently of the sizing cap.
    rm = RiskManager(client=client())
    account = Account(
        cleared_deposits=20.0,
        recent_sales=[Sale(proceeds=100.0, settlement_date=(NOW + timedelta(days=1)).date())],
    )
    decision = rm.evaluate(
        _viable_proposal(size_usd=120.0),
        account,
        NOW,
        account_equity=1000.0,
        high_water_mark=1000.0,
        daily_start_equity=1000.0,
        open_positions=[],
    )
    assert decision.approved is False
    assert decision.reason == "would_use_unsettled_funds"


def test_risk_manager_propagates_construction_rejection() -> None:
    rm = RiskManager(client=client())
    rejected = _viable_proposal()
    rejected.viable = False
    rejected.reason = "short_not_allowed_cash_account"
    decision = rm.evaluate(rejected, settled_account(1000.0), NOW, 500.0, 500.0, 500.0, [])
    assert decision.approved is False
    assert decision.reason == "short_not_allowed_cash_account"


def test_risk_manager_narrative_never_flips_decision() -> None:
    # Approve path stays approved regardless of the (offline, deterministic) narrative content.
    rm = RiskManager(client=client())
    d = rm.evaluate(_viable_proposal(30.0), settled_account(100.0), NOW, 500.0, 500.0, 500.0, [])
    assert d.approved is True
    assert isinstance(d.narrative_flag, str)
