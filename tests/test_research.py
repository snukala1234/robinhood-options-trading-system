"""Tests for Agent 2 (research sub-agents) and Agent 3 (edge aggregator)."""

from __future__ import annotations

from agents.calibration import BAND_080_100, RecalibrationStore
from agents.edge_aggregator import RESEARCH_KEYS, EdgeAggregator
from agents.research.base import ResearchAgent, ResearchContext
from agents.research.fundamental import FundamentalResearchAgent
from agents.research.macro import MacroResearchAgent
from agents.research.sentiment import SentimentResearchAgent
from agents.research.technical import TechnicalResearchAgent
from core.db import Database
from core.llm import ModelClient, OfflineProvider
from core.market_data import get_snapshot
from core.records import DIRECTIONS, ResearchSignal


def make_client(db: Database | None = None) -> ModelClient:
    return ModelClient(provider=OfflineProvider(), db=db)


def build_all(db: Database | None = None) -> list[ResearchAgent]:
    c = make_client(db)
    return [
        TechnicalResearchAgent(c, db),
        FundamentalResearchAgent(c, db),
        SentimentResearchAgent(c, db),
        MacroResearchAgent(c, db),
    ]


def make_ctx(symbol: str = "AAPL", regime: str = "normal") -> ResearchContext:
    return ResearchContext(symbol=symbol, snapshot=get_snapshot(symbol), market_regime=regime)


def test_each_research_agent_produces_valid_signal(db: Database) -> None:
    ctx = make_ctx()
    for agent in build_all(db):
        sig = agent.analyze(ctx)
        assert isinstance(sig, ResearchSignal)
        assert sig.direction in DIRECTIONS
        assert 0.0 <= sig.magnitude <= 1.0
        assert 0.0 <= sig.raw_confidence <= 1.0
        assert 0.0 <= sig.calibrated_confidence <= 1.0
        assert sig.source_agent == agent.agent_key
        assert sig.active_model == "claude-fable-5"


def test_research_output_is_deterministic_offline() -> None:
    ctx = make_ctx("NVDA")
    agent1 = TechnicalResearchAgent(client=make_client())
    agent2 = TechnicalResearchAgent(client=make_client())
    a = agent1.analyze(ctx)
    b = agent2.analyze(ctx)
    assert (a.direction, a.magnitude, a.raw_confidence) == (
        b.direction, b.magnitude, b.raw_confidence
    )


def test_every_research_output_persists_even_without_a_trade(db: Database) -> None:
    ctx = make_ctx("MSFT")
    for agent in build_all(db):
        agent.analyze(ctx)
    signals = db.recent_signals()
    assert len(signals) == 4
    assert all(s["resulted_in_trade"] == 0 for s in signals)


def test_recalibration_shifts_calibrated_confidence() -> None:
    ctx = make_ctx("TSLA")
    # Force a strong negative correction in the high band and confirm it lowers calibrated conf.
    base = TechnicalResearchAgent(client=make_client())
    base_sig = base.analyze(ctx)
    store = RecalibrationStore(deltas={("research_technical", BAND_080_100): -0.15,
                                       ("research_technical", "0.70-0.80"): -0.15,
                                       ("research_technical", "0.65-0.70"): -0.15,
                                       ("research_technical", "below_0.65"): -0.15})
    recal = TechnicalResearchAgent(client=make_client(), recal=store)
    recal_sig = recal.analyze(ctx)
    assert recal_sig.raw_confidence == base_sig.raw_confidence
    assert recal_sig.calibrated_confidence < base_sig.raw_confidence


def _run_research(db: Database, symbol: str, regime: str = "normal") -> list[ResearchSignal]:
    ctx = make_ctx(symbol, regime)
    return [agent.analyze(ctx) for agent in build_all(db)]


def test_aggregator_combines_four_signals(db: Database) -> None:
    signals = _run_research(db, "AAPL")
    snap = get_snapshot("AAPL")
    agg = EdgeAggregator(client=make_client(db), db=db).aggregate("AAPL", signals, snap, "normal")

    assert agg.symbol == "AAPL"
    assert agg.direction in DIRECTIONS
    assert 0.0 <= agg.calibrated_confidence <= 1.0
    assert set(agg.contributing.keys()) == set(RESEARCH_KEYS)
    # aggregator carries the market read for downstream sizing
    assert agg.current_price == snap.current_price
    assert agg.atr_14 == snap.atr_14
    # aggregator signal is also persisted
    persisted = [s for s in db.recent_signals() if s["source_agent"] == "edge_aggregator"]
    assert len(persisted) == 1


def test_aggregator_confidence_dampened_by_disagreement(db: Database) -> None:
    # Construct a maximally-split ensemble: 2 long, 2 short at high confidence.
    signals = [
        ResearchSignal("X", "research_technical", "long", 0.8, 0.9, 0.9, "r", "claude-fable-5"),
        ResearchSignal("X", "research_fundamental", "long", 0.8, 0.9, 0.9, "r", "claude-fable-5"),
        ResearchSignal("X", "research_sentiment", "short", 0.8, 0.9, 0.9, "r", "claude-fable-5"),
        ResearchSignal("X", "research_macro", "short", 0.8, 0.9, 0.9, "r", "claude-fable-5"),
    ]
    snap = get_snapshot("X")
    agg = EdgeAggregator(client=make_client(db), db=db).aggregate("X", signals, snap, "normal")
    # 50/50 split -> agreement 0.5 -> confidence roughly halved from ~0.9
    assert agg.calibrated_confidence < 0.6


def test_aggregator_propagates_failover_flag(db: Database) -> None:
    signals = [
        ResearchSignal("Y", "research_technical", "long", 0.7, 0.8, 0.8, "r",
                       "claude-opus-4-8", decided_under_failover=True),
        ResearchSignal("Y", "research_fundamental", "long", 0.6, 0.75, 0.75, "r",
                       "claude-fable-5"),
        ResearchSignal("Y", "research_sentiment", "long", 0.6, 0.7, 0.7, "r", "claude-fable-5"),
        ResearchSignal("Y", "research_macro", "flat", 0.4, 0.6, 0.6, "r", "claude-fable-5"),
    ]
    snap = get_snapshot("Y")
    agg = EdgeAggregator(client=make_client(db), db=db).aggregate("Y", signals, snap, "normal")
    assert agg.decided_under_failover is True
