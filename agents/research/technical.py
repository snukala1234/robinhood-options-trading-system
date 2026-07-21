"""Research sub-agent: technical (price action, volatility, volume)."""

from __future__ import annotations

from agents.research.base import ResearchAgent, ResearchContext
from core.records import FLAT, LONG, SHORT


class TechnicalResearchAgent(ResearchAgent):
    agent_key = "research_technical"

    def _heuristic(self, ctx: ResearchContext) -> dict[str, object]:
        snap = ctx.snapshot
        rng = self._rng(ctx.symbol)
        # Volume thrust vs. average is the primary offline tell; ATR gates confidence.
        if snap.volume_ratio >= 1.1:
            direction = LONG
        elif snap.volume_ratio <= 0.9:
            direction = SHORT
        else:
            direction = rng.choice([LONG, FLAT, SHORT])
        magnitude = min(1.0, abs(snap.volume_ratio - 1.0) + rng.uniform(0.1, 0.4))
        # Calmer names (lower ATR%) earn higher technical confidence.
        raw_conf = max(0.5, min(0.9, 0.85 - snap.atr_pct * 4 + rng.uniform(-0.05, 0.1)))
        reasoning = (
            f"vol {snap.volume_ratio:.2f}x avg, ATR {snap.atr_pct:.1%}; "
            f"{direction} bias on volume/volatility read"
        )
        return {
            "direction": direction,
            "magnitude": round(magnitude, 3),
            "raw_confidence": round(raw_conf, 3),
            "reasoning": reasoning,
        }

    def _prompt(self, ctx: ResearchContext) -> tuple[str, str]:
        snap = ctx.snapshot
        system = (
            "You are a disciplined technical analyst. Respond ONLY with a JSON object: "
            '{"direction": "long|short|flat", "magnitude": 0..1, '
            '"raw_confidence": 0..1, "reasoning": "..."}.'
        )
        user = (
            f"Symbol {ctx.symbol}. Price {snap.current_price}, ATR14 {snap.atr_14} "
            f"({snap.atr_pct:.2%} of price), volume {snap.volume:.0f} vs avg {snap.avg_volume:.0f} "
            f"(ratio {snap.volume_ratio:.2f}). Regime: {ctx.market_regime}. "
            "Give a short-horizon technical view."
        )
        return system, user
