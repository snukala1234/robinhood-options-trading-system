"""Research sub-agent: macro (regime / top-down)."""

from __future__ import annotations

from agents.research.base import ResearchAgent, ResearchContext
from core.market_data import REGIME_CALM, REGIME_VOLATILE
from core.records import FLAT, LONG, SHORT


class MacroResearchAgent(ResearchAgent):
    agent_key = "research_macro"

    def _heuristic(self, ctx: ResearchContext) -> dict[str, object]:
        # Macro view is dominated by the broad regime: calm favours risk-on (long),
        # volatile favours caution (flat/short). Deterministic jitter per symbol.
        rng = self._rng(ctx.symbol)
        if ctx.market_regime == REGIME_CALM:
            direction = LONG
            raw_conf = 0.7 + rng.uniform(0.0, 0.12)
        elif ctx.market_regime == REGIME_VOLATILE:
            direction = rng.choice([FLAT, SHORT])
            raw_conf = 0.6 + rng.uniform(0.0, 0.1)
        else:
            direction = rng.choice([LONG, FLAT])
            raw_conf = 0.6 + rng.uniform(0.0, 0.15)
        magnitude = min(1.0, 0.4 + rng.uniform(0.0, 0.4))
        reasoning = f"regime={ctx.market_regime}; top-down {direction} bias"
        return {
            "direction": direction,
            "magnitude": round(magnitude, 3),
            "raw_confidence": round(min(0.9, raw_conf), 3),
            "reasoning": reasoning,
        }

    def _prompt(self, ctx: ResearchContext) -> tuple[str, str]:
        system = (
            "You are a macro strategist. Respond ONLY with a JSON object: "
            '{"direction": "long|short|flat", "magnitude": 0..1, '
            '"raw_confidence": 0..1, "reasoning": "..."}.'
        )
        user = (
            f"Given market regime '{ctx.market_regime}', give a top-down view on holding "
            f"{ctx.symbol} over the next few sessions."
        )
        return system, user
