"""Agent 3 — Edge / Signal Aggregator.

Combines the four research sub-agent signals into one ensemble view. The numeric ensemble
(direction, calibrated confidence, weights) is deterministic pure-code arithmetic — per the
cross-cutting rule, model calls are reserved for genuine reasoning, so Fable 5 is used only
to synthesise the human-readable thesis narrative, not to do the weighting math. Ensemble
weights are the Section 3.3 lever Agent 8 tunes (down-weighting poorly-calibrated agents);
they default to equal and are loaded from the active strategy config.
"""

from __future__ import annotations

from core.event_bus import publish_agent_status, publish_signal_flow
from core.logging_setup import get_logger, log_decision
from core.records import FLAT, LONG, SHORT, AggregatedSignal, MarketSnapshot, ResearchSignal

_log = get_logger("edge_aggregator")

AGENT_KEY = "edge_aggregator"

RESEARCH_KEYS = (
    "research_technical",
    "research_fundamental",
    "research_sentiment",
    "research_macro",
)

DEFAULT_WEIGHTS: dict[str, float] = dict.fromkeys(RESEARCH_KEYS, 0.25)


class EdgeAggregator:
    def __init__(
        self,
        client: object,
        db: object | None = None,
        weights: dict[str, float] | None = None,
    ) -> None:
        self.client = client
        self.db = db
        self.weights = dict(weights) if weights else dict(DEFAULT_WEIGHTS)

    def aggregate(
        self,
        symbol: str,
        signals: list[ResearchSignal],
        snapshot: MarketSnapshot,
        market_regime: str,
    ) -> AggregatedSignal:
        publish_agent_status(AGENT_KEY, "running", f"aggregating {symbol}", active_model=None)

        # --- deterministic ensemble math (pure code) ---
        dir_weight = {LONG: 0.0, SHORT: 0.0}
        conf_weighted = {LONG: 0.0, SHORT: 0.0}
        mag_weighted = {LONG: 0.0, SHORT: 0.0}
        contributing: dict[str, dict[str, object]] = {}
        any_failover = False

        for sig in signals:
            weight = self.weights.get(sig.source_agent, 0.0)
            any_failover = any_failover or sig.decided_under_failover
            contributing[sig.source_agent] = {
                "raw_signal": sig.direction,
                "confidence": sig.calibrated_confidence,
                "weight": weight,
            }
            if sig.direction in (LONG, SHORT):
                dir_weight[sig.direction] += weight
                conf_weighted[sig.direction] += weight * sig.calibrated_confidence
                mag_weighted[sig.direction] += weight * sig.magnitude

        direction, agg_conf, magnitude = self._resolve(
            dir_weight, conf_weighted, mag_weighted, signals
        )

        # --- model call: synthesise the thesis narrative (genuine reasoning) ---
        offline_reasoning = self._offline_reasoning(symbol, direction, agg_conf, contributing)
        system, user = self._prompt(symbol, signals, market_regime, direction)
        result = self.client.complete_json(  # type: ignore[attr-defined]
            AGENT_KEY, system, user, {"reasoning": offline_reasoning}, agent_name=AGENT_KEY
        )
        reasoning = str(result.data.get("reasoning", offline_reasoning))
        decided_under_failover = any_failover or result.decided_under_failover

        aggregated = AggregatedSignal(
            symbol=symbol,
            direction=direction,
            magnitude=round(magnitude, 3),
            calibrated_confidence=round(agg_conf, 4),
            current_price=snapshot.current_price,
            atr_14=snapshot.atr_14,
            market_regime=market_regime,
            reasoning=reasoning,
            active_model=result.active_model,
            contributing=contributing,
            decided_under_failover=decided_under_failover,
        )

        self._persist(aggregated)
        log_decision(
            _log, "aggregated_signal", symbol=symbol, direction=direction,
            confidence=round(agg_conf, 4), model=result.active_model,
            failover=decided_under_failover,
        )
        publish_agent_status(
            AGENT_KEY, "idle", f"{symbol} {direction} conf {agg_conf:.2f}",
            active_model=result.active_model,
        )
        publish_signal_flow("edge_aggregator", symbol, f"{direction} {agg_conf:.2f}")
        return aggregated

    def _resolve(
        self,
        dir_weight: dict[str, float],
        conf_weighted: dict[str, float],
        mag_weighted: dict[str, float],
        signals: list[ResearchSignal],
    ) -> tuple[str, float, float]:
        """Pick the net direction and dampen confidence by cross-agent *disagreement*.

        Agreement is measured among agents that actually took a directional view — a FLAT
        agent is abstaining, not opposing, so it must not dilute the winning side's
        agreement. Denominator is therefore the directional weight (long+short), not the
        total weight including abstentions. This makes a strong directional read with a few
        abstentions tradeable, while a genuine long-vs-short split is still heavily dampened.
        """
        directional_weight = dir_weight[LONG] + dir_weight[SHORT]
        if directional_weight <= 0:
            mean_conf = (
                sum(s.calibrated_confidence for s in signals) / len(signals) if signals else 0.0
            )
            return FLAT, mean_conf * 0.5, 0.2

        direction = LONG if dir_weight[LONG] >= dir_weight[SHORT] else SHORT
        winning_weight = dir_weight[direction]
        agreement = winning_weight / directional_weight  # share among directional voters
        mean_conf = conf_weighted[direction] / winning_weight
        mean_mag = mag_weighted[direction] / winning_weight
        # Dampen confidence when directional voters split (avoids overconfident coin-flips).
        agg_conf = max(0.0, min(1.0, mean_conf * agreement))
        return direction, agg_conf, mean_mag

    def _offline_reasoning(
        self, symbol: str, direction: str, conf: float, contributing: dict[str, dict[str, object]]
    ) -> str:
        parts = []
        for agent, d in contributing.items():
            conf_val = d["confidence"]
            conf_f = float(conf_val) if isinstance(conf_val, int | float) else 0.0
            parts.append(f"{agent}={d['raw_signal']}({conf_f:.2f})")
        return f"Ensemble {direction} on {symbol} at {conf:.2f}: " + ", ".join(parts)

    def _prompt(
        self, symbol: str, signals: list[ResearchSignal], regime: str, direction: str
    ) -> tuple[str, str]:
        system = (
            "You synthesise multiple analysts into one thesis. The ensemble direction and "
            "confidence are already computed; do NOT change them. Respond ONLY with a JSON "
            'object: {"reasoning": "a 1-2 sentence synthesis"}.'
        )
        views = "; ".join(
            f"{s.source_agent}: {s.direction} (conf {s.calibrated_confidence:.2f}) - {s.reasoning}"
            for s in signals
        )
        user = (
            f"Symbol {symbol}. Regime {regime}. Ensemble direction: {direction}. "
            f"Analyst views: {views}. Write the synthesis."
        )
        return system, user

    def _persist(self, aggregated: AggregatedSignal) -> None:
        if self.db is None:
            return
        self.db.insert_signal(  # type: ignore[attr-defined]
            symbol=aggregated.symbol,
            source_agent=AGENT_KEY,
            direction=aggregated.direction,
            magnitude=aggregated.magnitude,
            raw_confidence=aggregated.calibrated_confidence,
            calibrated_confidence=aggregated.calibrated_confidence,
            reasoning=aggregated.reasoning,
            active_model=aggregated.active_model,
            decided_under_failover=aggregated.decided_under_failover,
            market_regime=aggregated.market_regime,
            resulted_in_trade=False,
        )
