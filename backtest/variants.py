"""Backtest-only research sub-agent variants (injected into the orchestrator by the engine).

These subclass the real ``ResearchAgent`` (core agent files are untouched) to change only the
DATA a sub-agent consumes, per the request to feed real fundamentals / neutralize the sentiment
noise. They still flow through the same aggregator, guardrails, sizing, and exits.
"""

from __future__ import annotations

from agents.research.base import ResearchAgent, ResearchContext
from core.records import FLAT, LONG, SHORT


class NeutralResearchAgent(ResearchAgent):
    """Emits no directional view (flat, low confidence). Used when a real signal is unavailable
    (e.g. sentiment, for which no look-ahead-free historical news data exists here)."""

    def __init__(self, client: object, db: object | None, agent_key: str) -> None:
        super().__init__(client, db)
        self.agent_key = agent_key

    def _heuristic(self, ctx: ResearchContext) -> dict[str, object]:
        return {"direction": FLAT, "magnitude": 0.0, "raw_confidence": 0.5,
                "reasoning": f"{self.agent_key} neutralized (no real point-in-time data)"}

    def _prompt(self, ctx: ResearchContext) -> tuple[str, str]:
        return ("Return a neutral view as JSON.", f"No signal for {ctx.symbol}.")


class RealFundamentalAgent(ResearchAgent):
    """Fundamental sub-agent driven by REAL point-in-time revenue growth (yfinance, lagged)."""

    agent_key = "research_fundamental"

    def __init__(self, client: object, db: object | None, store: object) -> None:
        super().__init__(client, db)
        self.store = store  # backtest.fundamentals.FundamentalsStore

    def _growth(self, ctx: ResearchContext) -> float | None:
        # ctx.snapshot.as_of is the point-in-time date; the store also enforces the lag.
        return self.store.growth_asof(ctx.symbol, ctx.snapshot.as_of)  # type: ignore[attr-defined]

    def _heuristic(self, ctx: ResearchContext) -> dict[str, object]:
        g = self._growth(ctx)
        if g is None:
            return {"direction": FLAT, "magnitude": 0.0, "raw_confidence": 0.5,
                    "reasoning": "no point-in-time fundamentals available as-of date"}
        if g > 0.03:
            direction = LONG
        elif g < -0.03:
            direction = SHORT
        else:
            direction = FLAT
        magnitude = min(1.0, abs(g) * 3 + 0.1)
        confidence = max(0.5, min(0.85, 0.6 + abs(g)))
        return {"direction": direction, "magnitude": round(magnitude, 3),
                "raw_confidence": round(confidence, 3),
                "reasoning": f"real revenue growth {g:+.1%} (point-in-time)"}

    def _prompt(self, ctx: ResearchContext) -> tuple[str, str]:
        g = self._growth(ctx)
        system = (
            "You are a fundamental analyst. Respond ONLY with a JSON object: "
            '{"direction": "long|short|flat", "magnitude": 0..1, '
            '"raw_confidence": 0..1, "reasoning": "..."}.'
        )
        growth_txt = f"{g:+.1%}" if g is not None else "unavailable"
        user = (
            f"{ctx.symbol}: point-in-time revenue growth (as of {ctx.snapshot.as_of}) = "
            f"{growth_txt}. Give a fundamental view for the next few weeks."
        )
        return system, user
