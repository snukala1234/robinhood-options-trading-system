"""Research sub-agent: fundamental (valuation / quality proxy)."""

from __future__ import annotations

from agents.research.base import ResearchAgent, ResearchContext
from core.records import FLAT, LONG, SHORT


class FundamentalResearchAgent(ResearchAgent):
    agent_key = "research_fundamental"

    def _heuristic(self, ctx: ResearchContext) -> dict[str, object]:
        # No live fundamentals on the offline path; use a deterministic per-symbol quality
        # score as the model stand-in. Online, the model performs the real analysis.
        rng = self._rng(ctx.symbol)
        quality = rng.uniform(-1.0, 1.0)  # negative = rich/weak, positive = cheap/strong
        if quality > 0.25:
            direction = LONG
        elif quality < -0.25:
            direction = SHORT
        else:
            direction = FLAT
        magnitude = min(1.0, abs(quality) + rng.uniform(0.05, 0.25))
        raw_conf = max(0.5, min(0.88, 0.6 + abs(quality) * 0.25 + rng.uniform(-0.05, 0.08)))
        reasoning = (
            f"quality/valuation score {quality:+.2f}; {direction} on fundamentals proxy"
        )
        return {
            "direction": direction,
            "magnitude": round(magnitude, 3),
            "raw_confidence": round(raw_conf, 3),
            "reasoning": reasoning,
        }

    def _prompt(self, ctx: ResearchContext) -> tuple[str, str]:
        system = (
            "You are a fundamental equity analyst. Respond ONLY with a JSON object: "
            '{"direction": "long|short|flat", "magnitude": 0..1, '
            '"raw_confidence": 0..1, "reasoning": "..."}.'
        )
        user = (
            f"Assess {ctx.symbol} on valuation and business quality for a short-to-medium "
            f"horizon. Current price {ctx.snapshot.current_price}. Regime: {ctx.market_regime}."
        )
        return system, user
