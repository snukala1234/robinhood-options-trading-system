"""Trade gate: precedence order, token minting/binding, committee boundary, audit rows."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import psycopg

from src.agents.schemas import PortfolioManagerDecision, RiskOfficerDecision
from src.config.risk_policy import GUARDRAIL_PRECEDENCE
from src.gate.kill_switches import KillSwitchPanel
from src.gate.trade_gate import (
    APPROVAL_TOKEN_TTL_SECONDS,
    TradeGate,
    hash_account_state,
    hash_quote_snapshot,
)
from tests.v2.gate_harness import NOW, make_account, make_input, make_quote

D = Decimal


def _gate(conn: psycopg.Connection[Any] | None = None) -> TradeGate:
    return TradeGate(panel=KillSwitchPanel(), conn=conn, clock=lambda: NOW)


def test_green_input_issues_a_fully_bound_token() -> None:
    gi = make_input()
    result = _gate().evaluate(gi)
    assert result.approved and result.token is not None
    assert result.quantity == 2  # 1% of 100k // 450
    token = result.token
    assert token.proposal_id == gi.proposal.proposal_id
    assert token.account_state_hash == hash_account_state(gi.account)
    assert token.quote_snapshot_hash == hash_quote_snapshot(gi.leg_quotes, gi.quote_snapshot_ids)
    assert token.halt_epoch == 0
    assert token.approved_quantity == 2
    assert token.limit_price == gi.proposal.limit_price
    assert token.total_max_loss == D("900")
    assert token.expires_at == NOW + timedelta(seconds=APPROVAL_TOKEN_TTL_SECONDS)
    assert token.correlation_id == gi.correlation_id


def test_steps_run_in_exact_section_3_1_order() -> None:
    result = _gate().evaluate(make_input())
    assert tuple(s.name for s in result.steps) == GUARDRAIL_PRECEDENCE
    assert [s.status for s in result.steps[:9]] == ["passed"] * 9
    assert result.steps[9].status == "delegated"


def test_committee_reductions_shrink_the_deterministic_quantity() -> None:
    gi = make_input(
        pm_decision=PortfolioManagerDecision(
            action="reduce_risk", risk_fraction_of_request="0.5", rationale="concentration"
        ),
        ro_decision=RiskOfficerDecision(
            decision="approve_with_reduction",
            reasons=["event risk"],
            reduction_fraction="0.8",
        ),
    )
    result = _gate().evaluate(gi)
    assert result.approved and result.token is not None
    assert result.quantity == 1  # budget 1000 * min(0.5, 0.8) = 500 -> 1 contract
    assert result.committee.effective_risk_fraction == D("0.5")


def test_veto_terminates_before_any_step_and_no_token_exists(
    conn: psycopg.Connection[Any],
) -> None:
    gi = make_input(
        ro_decision=RiskOfficerDecision(
            decision="veto", reasons=["breached limit: total_open_risk_pct"]
        )
    )
    result = _gate(conn).evaluate(gi)
    assert not result.approved and result.token is None
    assert result.rejection_step == "committee_aggregation"
    assert all(s.status == "not_evaluated" for s in result.steps)
    row = conn.execute("SELECT * FROM trade_proposals").fetchone()
    assert row is not None
    assert row["approval_status"] == "vetoed"
    assert row["risk_decision"]["committee"]["veto"] is True
    assert "risk_officer_veto" in row["risk_decision"]["rejection_reasons"]
    assert "breached limit: total_open_risk_pct" in row["risk_decision"]["rejection_reasons"]


def test_pm_hold_cash_yields_no_token_without_veto(conn: psycopg.Connection[Any]) -> None:
    gi = make_input(
        pm_decision=PortfolioManagerDecision(action="hold_cash", rationale="budget spent")
    )
    result = _gate(conn).evaluate(gi)
    assert not result.approved and result.token is None
    row = conn.execute("SELECT approval_status FROM trade_proposals").fetchone()
    assert row is not None
    assert row["approval_status"] == "rejected:committee_aggregation"


def test_approved_evaluation_is_recorded(conn: psycopg.Connection[Any]) -> None:
    gi = make_input()
    result = _gate(conn).evaluate(gi)
    assert result.token is not None
    row = conn.execute("SELECT * FROM trade_proposals").fetchone()
    assert row is not None
    assert row["approval_status"] == "approved"
    assert row["proposal"]["sized_quantity"] == 2
    assert row["proposal"]["limit_price"] == "4.50"
    assert row["risk_decision"]["token_id"] == str(result.token.token_id)
    assert [s["status"] for s in row["risk_decision"]["steps"][:9]] == ["passed"] * 9
    assert row["config_version_id"] == gi.proposal.config_version_id


def test_rejection_is_recorded_with_the_failing_step(conn: psycopg.Connection[Any]) -> None:
    stale = make_quote(observed_at=NOW - timedelta(seconds=30))
    result = _gate(conn).evaluate(make_input(leg_quotes=(stale,)))
    assert not result.approved
    row = conn.execute("SELECT * FROM trade_proposals").fetchone()
    assert row is not None
    assert row["approval_status"] == "rejected:system_health_and_data_freshness"
    assert any("quote is" in r for r in row["risk_decision"]["rejection_reasons"])


def test_token_binds_to_the_exact_account_and_quote_state() -> None:
    gi = make_input()
    result = _gate().evaluate(gi)
    assert result.token is not None
    other_account = make_account(total_equity=D("99999"))
    assert hash_account_state(other_account) != result.token.account_state_hash
    other_ids = (uuid.uuid4(),)
    assert hash_quote_snapshot(gi.leg_quotes, other_ids) != result.token.quote_snapshot_hash
