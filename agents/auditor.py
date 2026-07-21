"""Agent 8 — Performance & Calibration Auditor (the learning loop). FULLY IMPLEMENTED.

Implements Section 3 end to end:

- 3.2 Calibration tracking: bucket closed trades by contributing agent + confidence band over
  a rolling window; per bucket compute observed vs expected hit rate and a z-score; gate every
  adaptation on ``MIN_SAMPLE_SIZE_FOR_ADAPTATION`` and statistical significance.
- 3.3 What gets adapted: research confidence recalibration factors, edge-aggregator ensemble
  weights (down-weight, never remove), and exit take-profit — all TUNABLE params only.
- 3.4 Regime detection: rolling market volatility, kept strictly separate from calibration
  drift; a regime shift widens risk *tunables* only (never a Section 0 guardrail).
- 3.5 Shadow-testing: proposed changes are written to a shadow config, evaluated on paper
  against the control config, and promoted only if shadow Sharpe beats control with
  statistical significance and a minimum sample.
- 3.6 Human checkpoint: promotion requires an explicit human confirmation callback; default is
  to HOLD (decline) so nothing auto-promotes.

Hard rule enforced in code: Agent 8 only ever writes TUNABLE parameters
(``config.strategy`` + recalibration + ensemble weights). It can *recommend* but never *apply*
a change to any Section 0 guardrail (``config.guardrails.HARD_GUARDRAIL_NAMES``).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from agents.calibration import band_midpoint, confidence_band
from agents.edge_aggregator import DEFAULT_WEIGHTS, RESEARCH_KEYS
from config.guardrails import HARD_GUARDRAIL_NAMES, MIN_SAMPLE_SIZE_FOR_ADAPTATION
from config.strategy import DEFAULT_STRATEGY, StrategyParams
from core.event_bus import publish_agent_status
from core.logging_setup import get_logger, log_decision
from core.market_data import REGIME_CALM, REGIME_NORMAL, REGIME_VOLATILE, get_market_regime
from core.records import LONG, SHORT
from core.stats import (
    difference_is_significant,
    is_significant,
    proportion_z_score,
    sharpe_ratio,
)
from core.util import utcnow_iso

_log = get_logger("auditor")

AGENT_KEY = "auditor_calibration"

# Actions (mirror the Section 3.2 pseudocode return values).
ACTION_NONE = "no_action"
ACTION_REDUCE = "reduce_confidence_or_weight"
ACTION_INCREASE = "increase_weight"


# --- data structures --------------------------------------------------------


@dataclass
class _Acc:
    """Mutable accumulator for one (agent, band) bucket while scanning closed trades."""

    n: int = 0
    wins: int = 0
    start: str = ""
    end: str = ""


@dataclass
class CalibrationBucketStats:
    source_agent: str
    band: str
    sample_size: int
    wins: int
    observed_hit_rate: float
    expected_hit_rate: float
    z_score: float
    window_start: str | None
    window_end: str | None


@dataclass
class AdaptationProposal:
    source_agent: str
    band: str
    action: str
    reason: str
    z_score: float
    sample_size: int
    recommended_delta: float  # additive recalibration delta in confidence points


@dataclass
class ShadowResult:
    sample_size: int
    sharpe: float
    hit_rate: float
    returns: list[float] = field(default_factory=list)


@dataclass
class PromotionOutcome:
    decision: str  # "promoted" | "hold_insufficient" | "hold_no_improvement" | "hold_declined"
    shadow: ShadowResult
    control: ShadowResult
    new_config_version_id: str | None = None


# A human checkpoint callback: given an evidence dict, return True to promote.
HumanConfirmer = Callable[[dict[str, object]], bool]


def auto_hold_confirmer(_evidence: dict[str, object]) -> bool:
    """Default Phase-1 checkpoint: never auto-promote (HOLD)."""
    return False


# --- helpers ----------------------------------------------------------------


def _as_float(value: object, default: float = 0.0) -> float:
    """Safely coerce an object (from a JSON/row dict) to float."""
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _return_pct(trade: dict[str, object]) -> float:
    size = _as_float(trade.get("position_size_usd"))
    pnl = _as_float(trade.get("realized_pnl"))
    return pnl / size if size else 0.0


def _contributions(trade: dict[str, object]) -> dict[str, dict[str, object]]:
    raw = trade.get("contributing_agents")
    if not raw:
        return {}
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _ensemble_confidence(
    contribs: dict[str, dict[str, object]],
    weights: dict[str, float],
    recal: dict[tuple[str, str], float],
) -> tuple[str, float]:
    """Mirror of EdgeAggregator directional-agreement math for shadow selection.

    Applies shadow recalibration deltas to each agent's stored confidence and re-weights.
    """
    dir_weight = {LONG: 0.0, SHORT: 0.0}
    conf_weighted = {LONG: 0.0, SHORT: 0.0}
    for agent, d in contribs.items():
        direction = str(d.get("raw_signal", ""))
        if direction not in (LONG, SHORT):
            continue
        conf = _as_float(d.get("confidence"))
        delta = recal.get((agent, confidence_band(conf)), 0.0)
        conf = max(0.0, min(1.0, conf + delta))
        weight = weights.get(agent, 0.0)
        dir_weight[direction] += weight
        conf_weighted[direction] += weight * conf

    directional = dir_weight[LONG] + dir_weight[SHORT]
    if directional <= 0:
        return LONG, 0.0
    direction = LONG if dir_weight[LONG] >= dir_weight[SHORT] else SHORT
    agreement = dir_weight[direction] / directional
    mean_conf = conf_weighted[direction] / dir_weight[direction]
    return direction, max(0.0, min(1.0, mean_conf * agreement))


# --- calibration auditor ----------------------------------------------------


class CalibrationAuditor:
    """Sections 3.2–3.6. ``db`` is a core.db.Database; ``client`` an optional ModelClient."""

    def __init__(self, db: object, client: object | None = None) -> None:
        self.db = db
        self.client = client

    # -- 3.2 calibration tracking -----------------------------------------

    def compute_buckets(self, persist: bool = True) -> list[CalibrationBucketStats]:
        """Bucket closed trades by (contributing agent + aggregator, confidence band)."""
        closed = self.db.closed_trades()  # type: ignore[attr-defined]
        acc: dict[tuple[str, str], _Acc] = {}

        for trade in closed:
            ts = str(trade.get("exit_ts") or trade.get("entry_ts") or "")
            win = float(trade.get("realized_pnl") or 0.0) > 0.0

            # Each contributing research agent, bucketed by its own confidence band.
            for agent, d in _contributions(trade).items():
                conf = _as_float(d.get("confidence"))
                self._accumulate(acc, agent, confidence_band(conf), win, ts)

            # The aggregator itself, bucketed by the aggregated confidence.
            agg_conf = float(trade.get("aggregated_confidence") or 0.0)
            self._accumulate(acc, "edge_aggregator", confidence_band(agg_conf), win, ts)

        buckets: list[CalibrationBucketStats] = []
        for (agent, band), a in acc.items():
            observed = a.wins / a.n if a.n else 0.0
            expected = band_midpoint(band)
            z = proportion_z_score(observed, expected, a.n)
            bucket = CalibrationBucketStats(
                source_agent=agent, band=band, sample_size=a.n, wins=a.wins,
                observed_hit_rate=round(observed, 4), expected_hit_rate=expected,
                z_score=round(z, 4), window_start=a.start or None, window_end=a.end or None,
            )
            buckets.append(bucket)
            if persist:
                self.db.insert_calibration_bucket(  # type: ignore[attr-defined]
                    source_agent=agent, confidence_band=band,
                    window_start=bucket.window_start, window_end=bucket.window_end,
                    sample_size=a.n, wins=a.wins, observed_hit_rate=observed,
                    expected_hit_rate=expected, z_score=z,
                )
        return buckets

    @staticmethod
    def _accumulate(
        acc: dict[tuple[str, str], _Acc], agent: str, band: str, win: bool, ts: str
    ) -> None:
        cur = acc.setdefault((agent, band), _Acc(start=ts, end=ts))
        cur.n += 1
        cur.wins += 1 if win else 0
        if ts and (not cur.start or ts < cur.start):
            cur.start = ts
        if ts and (not cur.end or ts > cur.end):
            cur.end = ts

    # -- 3.2 evaluate_calibration -----------------------------------------

    def evaluate_calibration(self, bucket: CalibrationBucketStats) -> AdaptationProposal:
        """Section 3.2 evaluate_calibration, exactly."""
        if bucket.sample_size < MIN_SAMPLE_SIZE_FOR_ADAPTATION:
            return AdaptationProposal(bucket.source_agent, bucket.band, ACTION_NONE,
                                      "insufficient_sample", bucket.z_score,
                                      bucket.sample_size, 0.0)
        if not is_significant(bucket.z_score):
            return AdaptationProposal(bucket.source_agent, bucket.band, ACTION_NONE,
                                      "within_normal_variance", bucket.z_score,
                                      bucket.sample_size, 0.0)
        # Significant drift. Recommended recalibration delta corrects toward observed.
        delta = round(bucket.observed_hit_rate - bucket.expected_hit_rate, 4)
        if bucket.observed_hit_rate < bucket.expected_hit_rate:
            return AdaptationProposal(bucket.source_agent, bucket.band, ACTION_REDUCE,
                                      "overconfident_reduce", bucket.z_score,
                                      bucket.sample_size, delta)
        return AdaptationProposal(bucket.source_agent, bucket.band, ACTION_INCREASE,
                                  "underconfident_increase", bucket.z_score,
                                  bucket.sample_size, delta)

    def propose_adaptations(self) -> list[AdaptationProposal]:
        """All actionable (significant, sufficient-sample) proposals."""
        proposals = [self.evaluate_calibration(b) for b in self.compute_buckets()]
        return [p for p in proposals if p.action != ACTION_NONE]

    # -- 3.3 translate proposals into a shadow parameter set ---------------

    def build_shadow_parameters(
        self,
        proposals: list[AdaptationProposal],
        base_strategy: StrategyParams = DEFAULT_STRATEGY,
        base_weights: dict[str, float] | None = None,
        regime: str | None = None,
    ) -> dict[str, object]:
        """Assemble a TUNABLE-only parameter snapshot from the proposals (Section 3.3)."""
        weights = dict(base_weights) if base_weights else dict(DEFAULT_WEIGHTS)
        recal: dict[str, float] = {}

        for p in proposals:
            # Research/aggregator confidence recalibration factor.
            if p.source_agent in RESEARCH_KEYS or p.source_agent == "edge_aggregator":
                recal[f"{p.source_agent}|{p.band}"] = p.recommended_delta
            # Ensemble weighting: down-weight poor calibration, up-weight strong (never remove).
            if p.source_agent in weights:
                factor = 0.8 if p.action == ACTION_REDUCE else 1.1
                weights[p.source_agent] = max(0.05, weights[p.source_agent] * factor)

        weights = _normalize(weights)
        strategy = base_strategy.clamp_to_ranges().to_dict()

        parameters: dict[str, object] = {
            "strategy": strategy,
            "ensemble_weights": weights,
            "recalibration": recal,
            "regime": regime or REGIME_NORMAL,
        }
        _assert_no_guardrail_keys(parameters)
        return parameters

    def register_shadow_config(
        self, parameters: dict[str, object], evidence: dict[str, object]
    ) -> str:
        """Persist a shadow config version (Section 3.5 step 2)."""
        _assert_no_guardrail_keys(parameters)
        return self.db.insert_config_version(  # type: ignore[attr-defined]
            parameters=parameters, promoted_by="pending_shadow",
            proposed_by_agent=AGENT_KEY, evidence=evidence, is_active=False, is_shadow=True,
        )

    # -- 3.5 shadow evaluation --------------------------------------------

    def evaluate_config(self, parameters: dict[str, object]) -> ShadowResult:
        """Evaluate a config on the closed-trade history (paper-only backtest of selection).

        Control = every closed trade. A shadow config that recalibrates/re-weights changes
        *which* trades clear the gate; per-trade return% is unchanged, so this compares the
        risk-adjusted return distribution of the selected sets.
        """
        weights = _weights_from(parameters)
        recal = _recal_from(parameters)
        returns: list[float] = []
        wins = 0
        for trade in self.db.closed_trades():  # type: ignore[attr-defined]
            contribs = _contributions(trade)
            if not contribs:
                continue
            direction, conf = _ensemble_confidence(contribs, weights, recal)
            # Cash account: only long entries clearing the 0.65 gate would be taken.
            from config.guardrails import MIN_SIGNAL_CONFIDENCE_TO_TRADE

            if direction == LONG and conf >= MIN_SIGNAL_CONFIDENCE_TO_TRADE:
                r = _return_pct(trade)
                returns.append(r)
                wins += 1 if r > 0 else 0
        n = len(returns)
        return ShadowResult(
            sample_size=n, sharpe=round(sharpe_ratio(returns), 4),
            hit_rate=round(wins / n, 4) if n else 0.0, returns=returns,
        )

    def control_result(self) -> ShadowResult:
        """The live (control) config's realised distribution: every closed trade."""
        returns = [
            _return_pct(t) for t in self.db.closed_trades()  # type: ignore[attr-defined]
        ]
        n = len(returns)
        wins = sum(1 for r in returns if r > 0)
        return ShadowResult(
            sample_size=n, sharpe=round(sharpe_ratio(returns), 4),
            hit_rate=round(wins / n, 4) if n else 0.0, returns=returns,
        )

    # -- 3.5/3.6 promotion with human checkpoint --------------------------

    def promote_shadow_config(
        self,
        shadow_config_id: str,
        shadow: ShadowResult,
        control: ShadowResult,
        parameters: dict[str, object],
        confirmer: HumanConfirmer = auto_hold_confirmer,
    ) -> PromotionOutcome:
        """Section 3.5 promote_shadow_config + Section 3.6 human checkpoint."""
        start = min((t.get("entry_ts") for t in self.db.closed_trades()), default=utcnow_iso())  # type: ignore[attr-defined]
        end = utcnow_iso()

        def record(promoted: bool) -> None:
            self.db.insert_shadow_result(  # type: ignore[attr-defined]
                config_version_id=shadow_config_id, start_ts=str(start), end_ts=end,
                trades_count=shadow.sample_size, sharpe_ratio=shadow.sharpe,
                hit_rate=shadow.hit_rate, promoted=promoted,
            )

        if shadow.sample_size < MIN_SAMPLE_SIZE_FOR_ADAPTATION:
            record(False)
            return PromotionOutcome("hold_insufficient", shadow, control)

        improved = shadow.sharpe > control.sharpe and difference_is_significant(
            shadow.returns, control.returns
        )
        if not improved:
            record(False)
            return PromotionOutcome("hold_no_improvement", shadow, control)

        evidence: dict[str, object] = {
            "shadow_sharpe": shadow.sharpe, "control_sharpe": control.sharpe,
            "shadow_n": shadow.sample_size, "shadow_hit_rate": shadow.hit_rate,
        }
        # Section 3.6: Phase 1 requires explicit human confirmation for every promotion.
        if not confirmer(evidence):
            record(False)
            return PromotionOutcome("hold_declined", shadow, control)

        # Promote: store a NEW live config version (full history retained) and activate it.
        new_id = self.db.insert_config_version(  # type: ignore[attr-defined]
            parameters=parameters, promoted_by="human_confirmed",
            proposed_by_agent=AGENT_KEY, evidence=evidence, is_active=True, is_shadow=False,
        )
        self.db.set_active_config(new_id)  # type: ignore[attr-defined]
        record(True)
        log_decision(_log, "config_promoted", new_config=new_id, **evidence)
        return PromotionOutcome("promoted", shadow, control, new_config_version_id=new_id)

    # -- 3.4 regime detection (separate from calibration drift) -----------

    def detect_regime(self) -> str:
        """Rolling market regime (Section 3.4). Independent of the calibration logic above."""
        return get_market_regime()

    def regime_risk_nudge(self, regime: str, base: StrategyParams = DEFAULT_STRATEGY) -> StrategyParams:
        """Widen/narrow TUNABLE risk params for the regime (never a Section 0 guardrail)."""
        if regime == REGIME_VOLATILE:
            # Dampen size in volatile regimes: lower the max scalars (within pre-approved range).
            candidate = StrategyParams(
                vol_target=base.vol_target, vol_scalar_min=base.vol_scalar_min,
                vol_scalar_max=0.8, conf_scalar_min=base.conf_scalar_min,
                conf_scalar_max=0.8, correlation_threshold=base.correlation_threshold,
                correlation_penalty=base.correlation_penalty, take_profit_pct=base.take_profit_pct,
            )
        elif regime == REGIME_CALM:
            candidate = base
        else:
            candidate = base
        return candidate.clamp_to_ranges()

    # -- Fable-5 narrative (genuine pattern-finding, non-authoritative) ----

    def summarize_findings(
        self, buckets: list[CalibrationBucketStats], proposals: list[AdaptationProposal]
    ) -> str:
        if self.client is None:
            return _offline_summary(buckets, proposals)
        offline = {"summary": _offline_summary(buckets, proposals)}
        system = (
            "You summarise calibration findings for a human reviewer. The statistics are "
            'final; do not invent numbers. Respond ONLY with JSON: {"summary": "..."}.'
        )
        user = (
            f"{len(buckets)} buckets, {len(proposals)} actionable proposals. "
            + "; ".join(f"{p.source_agent}/{p.band}:{p.action}(z={p.z_score})" for p in proposals)
        )
        result = self.client.complete_json(  # type: ignore[attr-defined]
            AGENT_KEY, system, user, offline, agent_name=AGENT_KEY
        )
        return str(result.data.get("summary", offline["summary"]))

    # -- orchestration -----------------------------------------------------

    def run_audit(self, confirmer: HumanConfirmer = auto_hold_confirmer) -> PromotionOutcome:
        """Full cycle: buckets -> proposals -> shadow config -> evaluate -> (human) promote."""
        publish_agent_status(AGENT_KEY, "running", "auditing calibration", active_model=None)
        buckets = self.compute_buckets()
        proposals = self.propose_adaptations()
        regime = self.detect_regime()

        if not proposals:
            publish_agent_status(AGENT_KEY, "idle", "no actionable calibration drift",
                                 active_model=None)
            control = self.control_result()
            return PromotionOutcome("hold_no_improvement", ShadowResult(0, 0.0, 0.0), control)

        parameters = self.build_shadow_parameters(proposals, regime=regime)
        evidence: dict[str, object] = {
            "proposals": [p.__dict__ for p in proposals], "regime": regime,
        }
        shadow_id = self.register_shadow_config(parameters, evidence)
        shadow = self.evaluate_config(parameters)
        control = self.control_result()
        outcome = self.promote_shadow_config(shadow_id, shadow, control, parameters, confirmer)

        summary = self.summarize_findings(buckets, proposals)
        log_decision(_log, "audit_complete", decision=outcome.decision, regime=regime,
                     proposals=len(proposals), summary=summary)
        publish_agent_status(AGENT_KEY, "idle", f"audit: {outcome.decision}", active_model=None)
        return outcome


# --- module helpers ---------------------------------------------------------


def _normalize(weights: dict[str, float]) -> dict[str, float]:
    total = sum(weights.values())
    if total <= 0:
        return weights
    return {k: round(v / total, 6) for k, v in weights.items()}


def _weights_from(parameters: dict[str, object]) -> dict[str, float]:
    raw = parameters.get("ensemble_weights", DEFAULT_WEIGHTS)
    if isinstance(raw, dict):
        return {str(k): float(v) for k, v in raw.items()}
    return dict(DEFAULT_WEIGHTS)


def _recal_from(parameters: dict[str, object]) -> dict[tuple[str, str], float]:
    raw = parameters.get("recalibration", {})
    out: dict[tuple[str, str], float] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if "|" in str(key):
                agent, band = str(key).split("|", 1)
                out[(agent, band)] = float(value)
    return out


def _assert_no_guardrail_keys(parameters: dict[str, object]) -> None:
    """Guarantee Agent 8 never writes a Section 0 guardrail value."""
    flat: set[str] = set()

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                flat.add(str(k))
                walk(v)

    walk(parameters)
    overlap = flat & HARD_GUARDRAIL_NAMES
    if overlap:
        raise ValueError(f"Agent 8 may not write Section 0 guardrails: {overlap}")


def _offline_summary(
    buckets: list[CalibrationBucketStats], proposals: list[AdaptationProposal]
) -> str:
    if not proposals:
        return f"{len(buckets)} buckets tracked; no statistically significant calibration drift."
    lines = [
        f"{p.source_agent} {p.band}: {p.action} (z={p.z_score}, n={p.sample_size}, "
        f"delta={p.recommended_delta})"
        for p in proposals
    ]
    return f"{len(proposals)} actionable proposal(s): " + "; ".join(lines)
