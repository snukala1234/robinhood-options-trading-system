"""Tests for the real-fundamentals variant: point-in-time correctness + agent wiring."""

from __future__ import annotations

import pandas as pd

from agents.research.base import ResearchContext
from backtest.fundamentals import FundamentalsStore
from backtest.variants import NeutralResearchAgent, RealFundamentalAgent
from core.llm import ModelClient, OfflineProvider
from core.records import LONG, MarketSnapshot


def make_store() -> FundamentalsStore:
    ser = pd.Series(
        [100.0, 110.0, 120.0, 130.0, 145.0],
        index=pd.to_datetime(
            ["2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31", "2025-03-31"]
        ),
    )
    return FundamentalsStore({"AAA": ser})


def test_fundamentals_respect_reporting_lag_no_lookahead() -> None:
    store = make_store()
    # As of 2025-04-15, the 2025-03-31 quarter (public ~2025-05-15 with a 45d lag) is NOT yet
    # available -> only 4 quarters usable -> QoQ (130 vs 120).
    g_early = store.growth_asof("AAA", "2025-04-15")
    assert g_early is not None and abs(g_early - (130 - 120) / 120) < 1e-9
    # As of 2025-06-01, the 2025-03-31 quarter IS public -> 5 quarters -> YoY (145 vs 100).
    g_late = store.growth_asof("AAA", "2025-06-01")
    assert g_late is not None and abs(g_late - (145 - 100) / 100) < 1e-9


def test_fundamentals_none_when_insufficient_history() -> None:
    store = make_store()
    # Before any quarter is public, no signal.
    assert store.growth_asof("AAA", "2024-01-01") is None
    assert store.growth_asof("MISSING", "2025-06-01") is None


def _ctx(as_of: str) -> ResearchContext:
    snap = MarketSnapshot("AAA", 100.0, 2.0, 0.02, 1e6, 1e6, 1.0, as_of)
    return ResearchContext("AAA", snap, "normal")


def test_real_fundamental_agent_reflects_growth() -> None:
    agent = RealFundamentalAgent(ModelClient(provider=OfflineProvider()), None, make_store())
    sig = agent.analyze(_ctx("2025-06-01"))  # strong YoY growth -> long
    assert sig.source_agent == "research_fundamental"
    assert sig.direction == LONG
    assert "revenue growth" in sig.reasoning


def test_real_fundamental_agent_neutral_when_no_data() -> None:
    agent = RealFundamentalAgent(ModelClient(provider=OfflineProvider()), None, make_store())
    sig = agent.analyze(_ctx("2024-01-01"))  # no fundamentals public yet
    assert sig.direction == "flat" and sig.raw_confidence == 0.5


def test_neutral_agent_is_flat() -> None:
    agent = NeutralResearchAgent(
        ModelClient(provider=OfflineProvider()), None, "research_sentiment"
    )
    sig = agent.analyze(_ctx("2025-06-01"))
    assert sig.source_agent == "research_sentiment"
    assert sig.direction == "flat"
