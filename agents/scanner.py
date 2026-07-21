"""Agent 1 — Scanner.

The highest-volume agent: it narrows the universe to a ranked candidate list for the
research agents. A pure-code prefilter enforces a liquidity floor (an operational filter,
not a Section 0 guardrail); the Fable-5 call then ranks/selects which liquid names are worth
deeper research (genuine reasoning). Offline, a deterministic score stands in for the model.
"""

from __future__ import annotations

from dataclasses import dataclass

from config.settings import DEFAULT_UNIVERSE
from core.event_bus import publish_agent_status, publish_signal_flow
from core.logging_setup import get_logger, log_decision
from core.market_data import get_snapshots
from core.records import MarketSnapshot

_log = get_logger("scanner")

AGENT_KEY = "scanner"

# Liquidity floor for a Phase-1 candidate (operational filter; not a risk guardrail).
MIN_AVG_VOLUME = 500_000.0


@dataclass
class ScanCandidate:
    symbol: str
    snapshot: MarketSnapshot
    score: float
    rationale: str


class Scanner:
    def __init__(
        self,
        client: object,
        db: object | None = None,
        universe: list[str] | None = None,
        max_candidates: int = 8,
    ) -> None:
        self.client = client
        self.db = db
        self.universe = list(universe) if universe else list(DEFAULT_UNIVERSE)
        self.max_candidates = max_candidates

    @staticmethod
    def _score(snap: MarketSnapshot) -> float:
        """Deterministic candidate score: reward volume thrust, penalise extreme ATR."""
        atr = max(snap.atr_pct, 0.005)
        return round(snap.volume_ratio + (0.02 / atr) * 0.1, 4)

    def scan(self) -> list[ScanCandidate]:
        publish_agent_status(AGENT_KEY, "running", f"scanning {len(self.universe)} symbols",
                             active_model=None)
        snapshots = get_snapshots(self.universe)

        # --- pure-code liquidity prefilter ---
        liquid = {
            sym: snap
            for sym, snap in snapshots.items()
            if snap.avg_volume >= MIN_AVG_VOLUME and snap.current_price > 0
        }
        scored = sorted(liquid.items(), key=lambda kv: self._score(kv[1]), reverse=True)
        ranked_symbols = [sym for sym, _ in scored]

        # --- model call: rank/select candidates worth researching ---
        offline = {"ranked": ranked_symbols, "rationale": "ranked by liquidity/volatility score"}
        system, user = self._prompt(ranked_symbols)
        result = self.client.complete_json(  # type: ignore[attr-defined]
            AGENT_KEY, system, user, offline, agent_name=AGENT_KEY
        )
        model_ranked = result.data.get("ranked", ranked_symbols)
        if not isinstance(model_ranked, list):
            model_ranked = ranked_symbols
        # Keep only symbols we actually have a snapshot for, preserve model order.
        final = [s for s in model_ranked if s in liquid][: self.max_candidates]

        candidates = [
            ScanCandidate(
                symbol=sym,
                snapshot=liquid[sym],
                score=self._score(liquid[sym]),
                rationale=str(result.data.get("rationale", offline["rationale"])),
            )
            for sym in final
        ]

        log_decision(
            _log, "scan_complete", universe=len(self.universe), liquid=len(liquid),
            selected=[c.symbol for c in candidates], model=result.active_model,
            failover=result.decided_under_failover,
        )
        publish_agent_status(
            AGENT_KEY, "idle", f"selected {len(candidates)} candidates",
            active_model=result.active_model,
        )
        for c in candidates:
            publish_signal_flow("scanner", c.symbol, f"candidate score {c.score}")
        return candidates

    def _prompt(self, ranked_symbols: list[str]) -> tuple[str, str]:
        system = (
            "You are an equities scanner. From the pre-filtered liquid symbols, return the "
            "ones most worth deeper research. Respond ONLY with a JSON object: "
            '{"ranked": ["SYM", ...], "rationale": "..."}.'
        )
        user = f"Liquid candidates (already liquidity-filtered): {ranked_symbols}."
        return system, user
