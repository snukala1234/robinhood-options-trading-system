"""Agent 8 (learning loop) tests — calibration, significance, shadow-testing, human gate.

Proves the loop is fully implemented (not stubbed): buckets are computed, gated on sample
size + significance, translated into TUNABLE-only shadow params, evaluated vs control, and
promoted only with an explicit human confirmation. Guardrails are never writable by Agent 8.
"""

from __future__ import annotations

import pytest

from agents.auditor import (
    ACTION_NONE,
    ACTION_REDUCE,
    CalibrationAuditor,
    ShadowResult,
    auto_hold_confirmer,
)
from config.guardrails import HARD_GUARDRAIL_NAMES, MIN_SAMPLE_SIZE_FOR_ADAPTATION
from core.db import Database
from core.llm import ModelClient, OfflineProvider


def seed_trades(db: Database, agent: str, n: int, wins: int, confidence: float) -> None:
    """Seed n closed trades where `agent` (and the aggregator) had `confidence`; `wins` win."""
    for i in range(n):
        win = i < wins
        ret = 0.05 if win else -0.05
        size = 50.0
        contributing = {agent: {"raw_signal": "long", "confidence": confidence, "weight": 0.25}}
        tid = db.insert_trade_entry(
            symbol="AAPL",
            entry_price=100.0,
            position_size_usd=size,
            shares=0.5,
            contributing_agents=contributing,
            aggregated_confidence=confidence,
            account_equity_at_entry=200.0,
            atr_pct_at_entry=0.02,
            market_regime_at_entry="normal",
            stop_loss_pct=0.18,
            take_profit_pct=0.25,
            config_version_id=None,
            active_model="claude-fable-5",
        )
        db.close_trade(
            tid,
            exit_price=100.0 * (1 + ret),
            exit_reason="take_profit" if win else "stop_loss",
            realized_pnl=ret * size,
            holding_period_hours=5.0,
        )


def auditor(db: Database) -> CalibrationAuditor:
    return CalibrationAuditor(db=db, client=ModelClient(provider=OfflineProvider(), db=db))


def bucket_for(buckets, agent, band):  # noqa: ANN001
    return next(b for b in buckets if b.source_agent == agent and b.band == band)


# === 3.2 calibration tracking + significance ===============================


def test_significant_overconfidence_proposes_reduction(db: Database) -> None:
    # 40 high-confidence (0.85 -> band 0.80-1.00, expected 0.90) calls that win only 50%.
    seed_trades(db, "research_technical", n=40, wins=20, confidence=0.85)
    a = auditor(db)
    buckets = a.compute_buckets()
    b = bucket_for(buckets, "research_technical", "0.80-1.00")
    assert b.sample_size == 40
    assert b.observed_hit_rate == 0.5 and b.expected_hit_rate == 0.90
    proposal = a.evaluate_calibration(b)
    assert proposal.action == ACTION_REDUCE
    assert proposal.recommended_delta < 0  # correct confidence downward
    # persisted to calibration_buckets
    assert any(r["source_agent"] == "research_technical" for r in db.latest_calibration_buckets())


def test_insufficient_sample_blocks_adaptation(db: Database) -> None:
    seed_trades(db, "research_technical", n=10, wins=2, confidence=0.85)
    a = auditor(db)
    b = bucket_for(a.compute_buckets(), "research_technical", "0.80-1.00")
    assert b.sample_size < MIN_SAMPLE_SIZE_FOR_ADAPTATION
    assert a.evaluate_calibration(b).action == ACTION_NONE
    assert a.propose_adaptations() == []


def test_within_variance_no_action(db: Database) -> None:
    # 40 calls at 0.66 (band 0.65-0.70, expected 0.675) win 27/40 = 0.675 -> z ~ 0.
    seed_trades(db, "research_technical", n=40, wins=27, confidence=0.66)
    a = auditor(db)
    b = bucket_for(a.compute_buckets(), "research_technical", "0.65-0.70")
    proposal = a.evaluate_calibration(b)
    assert proposal.action == ACTION_NONE
    assert proposal.reason == "within_normal_variance"


# === 3.3 Agent 8 never writes a Section 0 guardrail ========================


def test_shadow_parameters_are_tunable_only(db: Database) -> None:
    seed_trades(db, "research_technical", n=40, wins=20, confidence=0.85)
    a = auditor(db)
    params = a.build_shadow_parameters(a.propose_adaptations())
    # No guardrail name may appear anywhere in the parameter snapshot.
    flat: set[str] = set()

    def walk(o: object) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                flat.add(str(k))
                walk(v)

    walk(params)
    assert flat.isdisjoint(HARD_GUARDRAIL_NAMES)
    assert "ensemble_weights" in params and "recalibration" in params


def test_registering_guardrail_key_is_rejected(db: Database) -> None:
    a = auditor(db)
    with pytest.raises(ValueError, match="may not write Section 0 guardrails"):
        a.register_shadow_config({"MAX_POSITION_PCT_OF_EQUITY": 0.9}, evidence={})


# === 3.5 / 3.6 shadow promotion + human checkpoint =========================


def _results() -> tuple[ShadowResult, ShadowResult]:
    control_returns = [-0.05] * 20 + [0.05] * 20  # mean 0
    shadow_returns = [0.05] * 30 + [0.06] * 10  # clearly higher, low variance
    control = ShadowResult(sample_size=40, sharpe=0.1, hit_rate=0.5, returns=control_returns)
    shadow = ShadowResult(sample_size=40, sharpe=0.9, hit_rate=1.0, returns=shadow_returns)
    return shadow, control


def test_promotion_requires_human_confirmation(db: Database) -> None:
    seed_trades(db, "research_technical", n=40, wins=20, confidence=0.85)
    a = auditor(db)
    params = a.build_shadow_parameters(a.propose_adaptations())
    shadow_id = a.register_shadow_config(params, evidence={})
    shadow, control = _results()

    # Default Phase-1 confirmer HOLDS (declines) -> not promoted.
    outcome = a.promote_shadow_config(shadow_id, shadow, control, params, auto_hold_confirmer)
    assert outcome.decision == "hold_declined"
    assert db.active_config() is None
    assert db.shadow_results()[-1]["promoted"] == 0


def test_promotion_with_human_yes_activates_new_config(db: Database) -> None:
    seed_trades(db, "research_technical", n=40, wins=20, confidence=0.85)
    a = auditor(db)
    params = a.build_shadow_parameters(a.propose_adaptations())
    shadow_id = a.register_shadow_config(params, evidence={})
    shadow, control = _results()

    outcome = a.promote_shadow_config(shadow_id, shadow, control, params, lambda _e: True)
    assert outcome.decision == "promoted"
    assert outcome.new_config_version_id is not None
    active = db.active_config()
    assert active is not None and active["promoted_by"] == "human_confirmed"
    assert db.shadow_results()[-1]["promoted"] == 1


def test_promotion_insufficient_shadow_sample_holds(db: Database) -> None:
    a = auditor(db)
    small = ShadowResult(sample_size=5, sharpe=0.9, hit_rate=1.0, returns=[0.05] * 5)
    control = ShadowResult(sample_size=40, sharpe=0.1, hit_rate=0.5, returns=[0.0] * 40)
    shadow_id = a.register_shadow_config({"strategy": {}}, evidence={})
    outcome = a.promote_shadow_config(shadow_id, small, control, {"strategy": {}}, lambda _e: True)
    assert outcome.decision == "hold_insufficient"


def test_no_improvement_holds(db: Database) -> None:
    a = auditor(db)
    # Shadow no better than control -> hold even with a yes-man confirmer.
    same = [0.01] * 40
    shadow = ShadowResult(40, 0.1, 0.5, same)
    control = ShadowResult(40, 0.1, 0.5, same)
    sid = a.register_shadow_config({"strategy": {}}, evidence={})
    outcome = a.promote_shadow_config(sid, shadow, control, {"strategy": {}}, lambda _e: True)
    assert outcome.decision == "hold_no_improvement"


# === 3.4 regime detection (separate from calibration) ======================


def test_regime_detection_and_conservative_nudge(db: Database) -> None:
    a = auditor(db)
    regime = a.detect_regime()
    assert regime in {"calm", "normal", "volatile"}
    nudged = a.regime_risk_nudge("volatile")
    # Volatile regime dampens the max scalars (tunable only), staying within pre-approved range.
    assert nudged.conf_scalar_max <= 0.8
    assert nudged.vol_scalar_max <= 0.8


# === end-to-end audit ======================================================


def test_run_audit_end_to_end_holds_without_human(db: Database) -> None:
    seed_trades(db, "research_technical", n=40, wins=20, confidence=0.85)
    a = auditor(db)
    outcome = a.run_audit()  # default auto-hold confirmer
    assert outcome.decision in {"hold_declined", "hold_no_improvement", "hold_insufficient"}
    # A shadow config was registered and buckets were persisted.
    shadow_versions = db.conn.execute(
        "SELECT COUNT(*) c FROM strategy_config_versions WHERE is_shadow = 1"
    ).fetchone()["c"]
    assert shadow_versions >= 1
    assert db.latest_calibration_buckets()
