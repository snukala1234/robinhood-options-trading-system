"""Research sub-agent: sentiment (news / flow tone proxy)."""

from __future__ import annotations

from agents.research.base import ResearchAgent, ResearchContext
from core.records import FLAT, LONG, SHORT


class SentimentResearchAgent(ResearchAgent):
    agent_key = "research_sentiment"

    def _heuristic(self, ctx: ResearchContext) -> dict[str, object]:
        # Deterministic per-symbol sentiment proxy; blended slightly with volume as a
        # crowd-attention signal so it is not purely independent of the tape.
        rng = self._rng(ctx.symbol)
        tone = rng.uniform(-1.0, 1.0) + (ctx.snapshot.volume_ratio - 1.0) * 0.3
        if tone > 0.2:
            direction = LONG
        elif tone < -0.2:
            direction = SHORT
        else:
            direction = FLAT
        magnitude = min(1.0, abs(tone) * 0.6 + rng.uniform(0.05, 0.25))
        raw_conf = max(0.5, min(0.85, 0.58 + abs(tone) * 0.18 + rng.uniform(-0.05, 0.08)))
        reasoning = f"sentiment tone {tone:+.2f} (news/flow proxy); {direction}"
        return {
            "direction": direction,
            "magnitude": round(magnitude, 3),
            "raw_confidence": round(raw_conf, 3),
            "reasoning": reasoning,
        }

    def _prompt(self, ctx: ResearchContext) -> tuple[str, str]:
        system = (
            "You are a market-sentiment analyst reading news and flow. Respond ONLY with a "
            'JSON object: {"direction": "long|short|flat", "magnitude": 0..1, '
            '"raw_confidence": 0..1, "reasoning": "..."}.'
        )
        user = (
            f"Gauge near-term sentiment for {ctx.symbol}. Volume ratio "
            f"{ctx.snapshot.volume_ratio:.2f}. Regime: {ctx.market_regime}."
        )
        return system, user
