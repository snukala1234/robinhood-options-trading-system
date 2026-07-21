"""Base class for Agent 2's research sub-agents.

Every sub-agent produces a structured :class:`ResearchSignal` (direction, magnitude,
raw + calibrated confidence, reasoning) through the Fable-5 model interface. On the offline
path a deterministic heuristic (computed from the real market snapshot) stands in for the
model so paper runs are reproducible; online, the same schema is requested from the model.
Every output is persisted to ``signal_history`` regardless of whether a trade results
(Section 6 step 2).
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass

from agents.calibration import EMPTY_RECALIBRATION, RecalibrationStore, apply_recalibration
from core.event_bus import publish_agent_status, publish_signal_flow
from core.logging_setup import get_logger, log_decision
from core.records import FLAT, LONG, SHORT, MarketSnapshot, ResearchSignal

_log = get_logger("research")

_VALID_DIRECTIONS = {LONG, SHORT, FLAT}


@dataclass
class ResearchContext:
    """Everything a research sub-agent needs to analyse one symbol."""

    symbol: str
    snapshot: MarketSnapshot
    market_regime: str


class ResearchAgent(ABC):
    """Common pipeline: heuristic/offline -> model call -> calibrate -> persist -> publish."""

    agent_key: str = "research_base"

    def __init__(
        self,
        client: object,
        db: object | None = None,
        recal: RecalibrationStore = EMPTY_RECALIBRATION,
    ) -> None:
        # ``client`` is a core.llm.ModelClient; typed as object to avoid an import cycle.
        self.client = client
        self.db = db
        self.recal = recal

    def _rng(self, symbol: str) -> random.Random:
        """Deterministic per-(agent, symbol) RNG for the offline heuristic stand-in."""
        seed = sum((i + 1) * ord(c) for i, c in enumerate(f"{self.agent_key}:{symbol}"))
        return random.Random(seed)

    @abstractmethod
    def _heuristic(self, ctx: ResearchContext) -> dict[str, object]:
        """Deterministic offline stand-in: {direction, magnitude, raw_confidence, reasoning}."""

    @abstractmethod
    def _prompt(self, ctx: ResearchContext) -> tuple[str, str]:
        """Return (system, user) prompts for the online model call."""

    def analyze(self, ctx: ResearchContext) -> ResearchSignal:
        publish_agent_status(self.agent_key, "running", f"analyzing {ctx.symbol}",
                             active_model=None)
        offline = self._heuristic(ctx)
        system, user = self._prompt(ctx)

        # ModelClient.complete_json — resolves the model from config.models (never hardcoded).
        result = self.client.complete_json(  # type: ignore[attr-defined]
            self.agent_key, system, user, offline, agent_name=self.agent_key
        )
        data = result.data

        direction = str(data.get("direction", offline["direction"]))
        if direction not in _VALID_DIRECTIONS:
            direction = FLAT
        magnitude = _clamp01(float(data.get("magnitude", offline["magnitude"])))
        raw_conf = _clamp01(float(data.get("raw_confidence", offline["raw_confidence"])))
        reasoning = str(data.get("reasoning", offline["reasoning"]))
        calibrated = apply_recalibration(self.recal, self.agent_key, raw_conf)

        signal = ResearchSignal(
            symbol=ctx.symbol,
            source_agent=self.agent_key,
            direction=direction,
            magnitude=magnitude,
            raw_confidence=raw_conf,
            calibrated_confidence=calibrated,
            reasoning=reasoning,
            active_model=result.active_model,
            decided_under_failover=result.decided_under_failover,
        )

        self._persist(ctx, signal)
        log_decision(
            _log, "research_signal", agent=self.agent_key, symbol=ctx.symbol,
            direction=direction, raw_conf=raw_conf, calibrated=calibrated,
            model=result.active_model, failover=result.decided_under_failover,
        )
        publish_agent_status(
            self.agent_key, "idle",
            f"{ctx.symbol} {direction} conf {calibrated:.2f}", active_model=result.active_model,
        )
        publish_signal_flow("research", ctx.symbol, f"{self.agent_key}:{direction} {calibrated:.2f}")
        return signal

    def _persist(self, ctx: ResearchContext, signal: ResearchSignal) -> None:
        if self.db is None:
            return
        self.db.insert_signal(  # type: ignore[attr-defined]
            symbol=signal.symbol,
            source_agent=signal.source_agent,
            direction=signal.direction,
            magnitude=signal.magnitude,
            raw_confidence=signal.raw_confidence,
            calibrated_confidence=signal.calibrated_confidence,
            reasoning=signal.reasoning,
            active_model=signal.active_model,
            decided_under_failover=signal.decided_under_failover,
            market_regime=ctx.market_regime,
            resulted_in_trade=False,
        )


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))
