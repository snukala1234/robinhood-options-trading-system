"""Section 13.4 end to end: calibration runs, sample gates, shadow isolation,
control comparison, human promotion, rollback."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from src.agents.schemas import CalibrationReport, TunableProposal
from src.config.tunables import DEFAULT_TUNABLES, TunableParams
from src.learning.calibration import evidence_for_auditor, run_calibration
from src.learning.promotion import (
    PromotionError,
    assert_tunable_allowed,
    compare_to_control,
    promote,
    propose_change,
    reject_shadow,
    rollback_active,
)
from src.learning.shadow import (
    SCORE_COMPONENTS,
    ShadowCandidate,
    ShadowEvaluator,
    record_shadow_decision,
)
from src.orchestration.config_integrity import stamped_evidence, verify_config_row
from src.persistence.repositories import ConfigVersionRepository
from tests.v2.test_learning_metrics import make_record

D = Decimal
SRC = Path(__file__).resolve().parents[2] / "src"
WINDOW_START = datetime(2026, 6, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 7, 22, tzinfo=UTC)


def _seed_control(conn: psycopg.Connection[Any]) -> uuid.UUID:
    repo = ConfigVersionRepository(conn)
    params = DEFAULT_TUNABLES.to_dict()
    version_id = repo.insert_version(params, status="shadow", evidence=stamped_evidence(params))
    repo.transition(version_id, "active", approved_by="human-operator")
    return version_id


def _good_report(sample_size: int = 100, value: float = 0.40) -> CalibrationReport:
    return CalibrationReport(
        findings=["long_call 7-14d bucket shows negative expectancy"],
        proposals=[
            TunableProposal(
                parameter="profit_target_pct_of_max_gain",
                proposed_value=value,
                evidence_summary="negative expectancy over a qualified sample",
                sample_size=sample_size,
            )
        ],
        hold_reason=None,
    )


# --- calibration runs ---------------------------------------------------------


def test_calibration_persists_every_bucket_and_gates_evidence(
    conn: psycopg.Connection[Any],
) -> None:
    records = [make_record() for _ in range(35)] + [
        make_record(strategy="long_put", net_delta_per_unit=D("-0.40")) for _ in range(5)
    ]
    buckets = run_calibration(conn, records, window_start=WINDOW_START, window_end=WINDOW_END)
    by_key = {(b.dimension, b.bucket): b for b in buckets}
    assert by_key[("strategy", "long_call")].qualified
    assert by_key[("strategy", "long_call")].sample_size == 35
    assert not by_key[("strategy", "long_put")].qualified

    rows = conn.execute(
        "SELECT * FROM calibration_results WHERE dimension_key->>'dimension' = 'strategy'"
    ).fetchall()
    assert {r["dimension_key"]["bucket"]: r["sample_size"] for r in rows} == {
        "long_call": 35,
        "long_put": 5,
    }
    assert all(r["metrics"]["win_rate"] is not None for r in rows)

    evidence = evidence_for_auditor(buckets)
    assert evidence  # qualified buckets exist
    assert all(stat.sample_size >= 30 for stat in evidence)
    dims = {stat.dimension for stat in evidence}
    assert "strategy=long_call" in dims
    assert "strategy=long_put" not in dims  # under-sampled: data, never evidence


# --- sample-size and boundedness gates ----------------------------------------


def test_below_minimum_sample_cannot_generate_a_proposal(
    conn: psycopg.Connection[Any],
) -> None:
    with pytest.raises(PromotionError, match="not automatically an error"):
        propose_change(conn, _good_report(sample_size=10), base_params=DEFAULT_TUNABLES)
    count = conn.execute("SELECT count(*) AS n FROM strategy_config_versions").fetchone()
    assert count is not None and count["n"] == 0  # nothing was created


def test_out_of_bounds_value_is_refused(conn: psycopg.Connection[Any]) -> None:
    with pytest.raises(PromotionError, match="pre-approved clamp ranges"):
        propose_change(conn, _good_report(value=0.90), base_params=DEFAULT_TUNABLES)


def test_calibration_layer_reenforces_guardrails() -> None:
    """The schema already refuses guardrail names; the promotion layer refuses
    again in pure code — two independent layers."""
    with pytest.raises(PromotionError, match="hard guardrail"):
        assert_tunable_allowed("MAX_RISK_PER_TRADE_PCT")
    with pytest.raises(PromotionError, match="not a pre-approved tunable"):
        assert_tunable_allowed("brand_new_knob")


def test_empty_report_proposes_nothing(conn: psycopg.Connection[Any]) -> None:
    report = CalibrationReport(
        findings=[], proposals=[], hold_reason="insufficient samples everywhere"
    )
    assert propose_change(conn, report, base_params=DEFAULT_TUNABLES) is None


# --- shadow lifecycle ---------------------------------------------------------


def test_proposal_creates_an_immutable_hashed_shadow_version(
    conn: psycopg.Connection[Any],
) -> None:
    _seed_control(conn)
    version_id = propose_change(conn, _good_report(), base_params=DEFAULT_TUNABLES)
    assert version_id is not None
    row = ConfigVersionRepository(conn).get(version_id)
    assert row is not None
    assert row["status"] == "shadow"
    assert row["proposed_by"] == "performance_auditor"
    assert row["parameters"]["profit_target_pct_of_max_gain"] == 0.40
    ok, detail = verify_config_row(row)
    assert ok, detail
    assert row["evidence"]["proposals"][0]["sample_size"] == 100
    event = conn.execute(
        "SELECT * FROM system_events WHERE event_type = 'shadow_config_created'"
    ).fetchone()
    assert event is not None


def test_shadow_and_control_score_the_same_candidate_differently(
    conn: psycopg.Connection[Any],
) -> None:
    control_id = _seed_control(conn)
    report = CalibrationReport(
        findings=["directional edge underweighted in trending regimes"],
        proposals=[
            TunableProposal(
                parameter="weight_directional_edge",
                proposed_value=20.0,
                evidence_summary="qualified sample",
                sample_size=60,
            )
        ],
        hold_reason=None,
    )
    shadow_id = propose_change(conn, report, base_params=DEFAULT_TUNABLES)
    assert shadow_id is not None
    shadow_row = ConfigVersionRepository(conn).get(shadow_id)
    assert shadow_row is not None
    shadow_params = TunableParams.from_dict(dict(shadow_row["parameters"]))

    candidate = ShadowCandidate(
        candidate_id=uuid.uuid4(),
        components={name: D("0.74") for name in SCORE_COMPONENTS},
    )
    control_decision = ShadowEvaluator(control_id, DEFAULT_TUNABLES).evaluate(candidate)
    shadow_decision = ShadowEvaluator(shadow_id, shadow_params).evaluate(candidate)
    assert control_decision.score == D("74.0000")  # 100 total weight x 0.74
    assert not control_decision.would_enter  # below MIN_OPPORTUNITY_SCORE 75
    assert shadow_decision.score == D("77.7000")  # 105 total weight x 0.74
    assert shadow_decision.would_enter

    record_shadow_decision(conn, shadow_decision, window_start=WINDOW_START, window_end=WINDOW_END)
    row = conn.execute(
        "SELECT * FROM calibration_results WHERE dimension_key->>'type' = 'shadow_evaluation'"
    ).fetchone()
    assert row is not None
    assert row["metrics"]["would_enter"] is True

    # And through all of that: not one order anywhere.
    orders = conn.execute("SELECT count(*) AS n FROM orders").fetchone()
    assert orders is not None and orders["n"] == 0


def test_shadow_package_has_no_path_to_execution() -> None:
    """Structural: nothing under src/learning imports the gate, the execution
    layer, or a broker — a shadow config cannot reach an order code path."""
    offenders: list[str] = []
    forbidden = ("src.execution", "src.gate", "src.positions", "broker", "anthropic")
    for path in (SRC / "learning").rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            if any(term in stripped for term in forbidden):
                offenders.append(f"{path.name}: {stripped}")
    assert offenders == []


def test_a_shadow_decision_is_not_an_approval_token(conn: psycopg.Connection[Any]) -> None:
    from src.execution.order_state_machine import OrderStateMachine
    from src.execution.paper_broker import PaperBroker
    from src.execution.submission import OrderSubmitter, SubmissionRefused
    from src.gate.kill_switches import KillSwitchPanel
    from tests.v2.gate_harness import make_input

    decision = ShadowEvaluator(uuid.uuid4(), DEFAULT_TUNABLES).evaluate(
        ShadowCandidate(
            candidate_id=uuid.uuid4(),
            components={name: D("0.9") for name in SCORE_COMPONENTS},
        )
    )
    gi = make_input()
    submitter = OrderSubmitter(
        broker=PaperBroker(starting_cash=D("50000")),
        machine=OrderStateMachine(conn),
        panel=KillSwitchPanel(),
    )
    from src.domain.instruments import LegSide
    from src.execution.interface import LimitOrderRequest, NetIntent, OrderLeg
    from tests.v2.gate_harness import CALL_600

    request = LimitOrderRequest(
        idempotency_key=f"{gi.proposal.proposal_id}:1",
        underlying="SPY",
        legs=(OrderLeg(contract=CALL_600, side=LegSide.BUY, quantity=1),),
        limit_price=D("4.50"),
        net_intent=NetIntent.DEBIT,
        quantity=1,
    )
    with pytest.raises(SubmissionRefused) as exc_info:
        submitter.submit_entry(
            decision,  # type: ignore[arg-type]
            request,
            account=gi.account,
            leg_quotes=gi.leg_quotes,
            quote_snapshot_ids=gi.quote_snapshot_ids,
        )
    assert exc_info.value.reason == "no_approval_token"


# --- control comparison -------------------------------------------------------


def _spread_sample(low: str, high: str, n_each: int) -> list[Decimal]:
    return [D(low)] * n_each + [D(high)] * n_each


def test_comparison_gates_small_samples_and_short_windows() -> None:
    result = compare_to_control(
        _spread_sample("0", "20", 5),
        _spread_sample("0", "20", 5),
        window_days=3,
    )
    assert not result.eligible_for_review
    assert any("shadow sample" in r for r in result.reasons)
    assert any("window" in r for r in result.reasons)


def test_comparison_hand_case_is_significant_and_favorable() -> None:
    shadow = _spread_sample("20", "40", 20)  # mean 30
    control = _spread_sample("0", "20", 20)  # mean 10
    result = compare_to_control(shadow, control, window_days=14)
    assert result.shadow_expectancy == D("30.00")
    assert result.control_expectancy == D("10.00")
    assert result.difference == D("20.00")
    assert result.standard_error is not None
    assert D("2.26") < result.standard_error < D("2.27")  # sqrt(2 * (4000/39)/40)
    assert result.significant and result.favorable and result.eligible_for_review


def test_noise_is_not_significant() -> None:
    shadow = _spread_sample("-20", "22", 20)  # mean 1
    control = _spread_sample("-20", "20", 20)  # mean 0
    result = compare_to_control(shadow, control, window_days=14)
    assert not result.significant
    assert not result.eligible_for_review


# --- promotion and rollback ---------------------------------------------------


def _eligible_comparison() -> Any:
    return compare_to_control(
        _spread_sample("20", "40", 20), _spread_sample("0", "20", 20), window_days=14
    )


def test_promotion_requires_a_named_human(conn: psycopg.Connection[Any]) -> None:
    _seed_control(conn)
    version_id = propose_change(conn, _good_report(), base_params=DEFAULT_TUNABLES)
    assert version_id is not None
    with pytest.raises(PromotionError, match="named human"):
        promote(conn, version_id, comparison=_eligible_comparison(), approved_by="")
    row = ConfigVersionRepository(conn).get(version_id)
    assert row is not None and row["status"] == "shadow"  # unchanged


def test_promotion_refuses_ineligible_or_unfavorable_comparisons(
    conn: psycopg.Connection[Any],
) -> None:
    _seed_control(conn)
    version_id = propose_change(conn, _good_report(), base_params=DEFAULT_TUNABLES)
    assert version_id is not None
    small = compare_to_control(
        _spread_sample("20", "40", 5), _spread_sample("0", "20", 5), window_days=14
    )
    with pytest.raises(PromotionError, match="not cleared its gates"):
        promote(conn, version_id, comparison=small, approved_by="human")
    unfavorable = compare_to_control(
        _spread_sample("0", "20", 20), _spread_sample("20", "40", 20), window_days=14
    )
    with pytest.raises(PromotionError, match="did not outperform"):
        promote(conn, version_id, comparison=unfavorable, approved_by="human")


def test_promotion_and_rollback_round_trip(conn: psycopg.Connection[Any]) -> None:
    control_id = _seed_control(conn)
    version_id = propose_change(conn, _good_report(), base_params=DEFAULT_TUNABLES)
    assert version_id is not None

    promote(conn, version_id, comparison=_eligible_comparison(), approved_by="human-operator")
    repo = ConfigVersionRepository(conn)
    active = repo.active_version()
    assert active is not None and active["id"] == version_id
    assert active["approved_by"] == "human-operator"
    promoted_event = conn.execute(
        "SELECT * FROM system_events WHERE event_type = 'config_promoted'"
    ).fetchone()
    assert promoted_event is not None
    assert promoted_event["payload"]["comparison"]["significant"] is True

    restored = rollback_active(conn, rolled_back_by="human-operator", reason="regression")
    assert restored == control_id
    active_after = repo.active_version()
    assert active_after is not None and active_after["id"] == control_id
    rolled_row = repo.get(version_id)
    assert rolled_row is not None and rolled_row["status"] == "rolled_back"
    rollback_event = conn.execute(
        "SELECT * FROM system_events WHERE event_type = 'config_rolled_back'"
    ).fetchone()
    assert rollback_event is not None


def test_rollback_never_leaves_zero_active_configs(conn: psycopg.Connection[Any]) -> None:
    _seed_control(conn)  # exactly one active
    with pytest.raises(PromotionError, match="exactly one known-good active"):
        rollback_active(conn, rolled_back_by="human", reason="test")
    active = ConfigVersionRepository(conn).active_version()
    assert active is not None  # still there


def test_rejection_is_logged(conn: psycopg.Connection[Any]) -> None:
    _seed_control(conn)
    version_id = propose_change(conn, _good_report(), base_params=DEFAULT_TUNABLES)
    assert version_id is not None
    reject_shadow(conn, version_id, reason="no edge over control")
    row = ConfigVersionRepository(conn).get(version_id)
    assert row is not None and row["status"] == "rejected"
    event = conn.execute(
        "SELECT * FROM system_events WHERE event_type = 'config_rejected'"
    ).fetchone()
    assert event is not None and event["payload"]["reason"] == "no edge over control"
