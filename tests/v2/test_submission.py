"""Token-enforced submission: bindings, replay, and the kill-switch epoch check.

Integration proof of audit finding 1: a token that was perfectly valid at
issuance fails closed at the adapter when a kill switch trips in between —
and stays dead after the switch is cleared, because clearing bumps the epoch.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from src.domain.instruments import LegSide
from src.execution.interface import (
    DuplicateOrderError,
    LimitOrderRequest,
    NetIntent,
    OrderLeg,
)
from src.execution.order_state_machine import OrderStateMachine
from src.execution.paper_broker import PaperBroker
from src.execution.submission import OrderSubmitter, SubmissionRefused
from src.gate.kill_switches import KillSwitchPanel
from src.gate.trade_gate import GateInput, GateResult, TradeGate
from tests.v2.gate_harness import CALL_600, NOW, make_account, make_input

D = Decimal
SRC = Path(__file__).resolve().parents[2] / "src"


def _approved(
    conn: psycopg.Connection[Any], panel: KillSwitchPanel, **overrides: Any
) -> tuple[GateInput, GateResult]:
    gi = make_input(**overrides)
    result = TradeGate(panel=panel, conn=conn, clock=lambda: NOW).evaluate(gi)
    assert result.approved and result.token is not None
    return gi, result


def _request(gi: GateInput, quantity: int, attempt: int = 1) -> LimitOrderRequest:
    return LimitOrderRequest(
        idempotency_key=f"{gi.proposal.proposal_id}:{attempt}",
        underlying="SPY",
        legs=(OrderLeg(contract=CALL_600, side=LegSide.BUY, quantity=1),),
        limit_price=gi.proposal.limit_price,
        net_intent=NetIntent.DEBIT,
        quantity=quantity,
    )


def _submitter(
    conn: psycopg.Connection[Any], panel: KillSwitchPanel, *, at: Any = None
) -> tuple[OrderSubmitter, OrderStateMachine]:
    machine = OrderStateMachine(conn)
    submitter = OrderSubmitter(
        broker=PaperBroker(starting_cash=D("50000"), clock=lambda: NOW),
        machine=machine,
        panel=panel,
        clock=(lambda: at) if at is not None else (lambda: NOW),
    )
    return submitter, machine


def test_happy_path_walks_the_full_section_12_2_entry_path(
    conn: psycopg.Connection[Any],
) -> None:
    panel = KillSwitchPanel()
    gi, result = _approved(conn, panel)
    assert result.token is not None
    submitter, machine = _submitter(conn, panel)
    receipt = submitter.submit_entry(
        result.token,
        _request(gi, result.quantity),
        account=gi.account,
        leg_quotes=gi.leg_quotes,
        quote_snapshot_ids=gi.quote_snapshot_ids,
    )
    assert receipt.ack.state.value == "OPEN"
    states = [str(e["new_state"]) for e in machine.events(receipt.order_id)]
    assert states == ["CREATED", "VALIDATED", "STAGED", "SUBMITTED", "OPEN"]
    row = machine.get_order(receipt.order_id)
    assert row is not None and row["broker_order_id"] == receipt.ack.broker_order_id


def test_kill_switch_between_issuance_and_submit_fails_closed(
    conn: psycopg.Connection[Any],
) -> None:
    """Audit finding 1, end to end: valid token, then a switch trips, then submit."""
    panel = KillSwitchPanel()
    gi, result = _approved(conn, panel)
    assert result.token is not None and result.token.halt_epoch == 0
    panel.activate("order_state_uncertainty", reason="reconciliation mismatch")

    submitter, machine = _submitter(conn, panel)
    key = f"{gi.proposal.proposal_id}:1"
    with pytest.raises(SubmissionRefused) as exc_info:
        submitter.submit_entry(
            result.token,
            _request(gi, result.quantity),
            account=gi.account,
            leg_quotes=gi.leg_quotes,
            quote_snapshot_ids=gi.quote_snapshot_ids,
        )
    assert exc_info.value.reason == "kill_switch_active"
    order = machine.order_by_idempotency_key(key)
    assert order is not None and str(order["current_state"]) == "CANCELED"

    # Clearing the switch does NOT resurrect the token: the epoch moved twice.
    panel.clear("order_state_uncertainty", resumed_by="operator")
    assert panel.blocks_new_entries() == ()
    with pytest.raises(SubmissionRefused) as exc_info:
        submitter.submit_entry(
            result.token,
            _request(gi, result.quantity, attempt=2),
            account=gi.account,
            leg_quotes=gi.leg_quotes,
            quote_snapshot_ids=gi.quote_snapshot_ids,
        )
    assert exc_info.value.reason == "halt_epoch_changed"

    # Only a fresh pass through the gate, under the new epoch, can submit.
    gi2, result2 = _approved(conn, panel)
    assert result2.token is not None and result2.token.halt_epoch == 2
    receipt = submitter.submit_entry(
        result2.token,
        _request(gi2, result2.quantity),
        account=gi2.account,
        leg_quotes=gi2.leg_quotes,
        quote_snapshot_ids=gi2.quote_snapshot_ids,
    )
    assert receipt.ack.state.value == "OPEN"


def test_token_cannot_be_replayed_against_a_different_proposal(
    conn: psycopg.Connection[Any],
) -> None:
    panel = KillSwitchPanel()
    gi_a, result_a = _approved(conn, panel)
    gi_b, _ = _approved(conn, panel)
    assert result_a.token is not None
    submitter, _ = _submitter(conn, panel)
    with pytest.raises(SubmissionRefused) as exc_info:
        submitter.submit_entry(
            result_a.token,
            _request(gi_b, result_a.quantity),  # proposal B's order, proposal A's token
            account=gi_b.account,
            leg_quotes=gi_b.leg_quotes,
            quote_snapshot_ids=gi_b.quote_snapshot_ids,
        )
    assert exc_info.value.reason == "token_proposal_mismatch"


def test_token_dies_when_account_state_changes(conn: psycopg.Connection[Any]) -> None:
    panel = KillSwitchPanel()
    gi, result = _approved(conn, panel)
    assert result.token is not None
    submitter, _ = _submitter(conn, panel)
    with pytest.raises(SubmissionRefused) as exc_info:
        submitter.submit_entry(
            result.token,
            _request(gi, result.quantity),
            account=make_account(total_equity=D("90000")),  # equity moved
            leg_quotes=gi.leg_quotes,
            quote_snapshot_ids=gi.quote_snapshot_ids,
        )
    assert exc_info.value.reason == "account_state_mismatch"


def test_token_dies_when_quote_snapshot_changes(conn: psycopg.Connection[Any]) -> None:
    panel = KillSwitchPanel()
    gi, result = _approved(conn, panel)
    assert result.token is not None
    submitter, _ = _submitter(conn, panel)
    with pytest.raises(SubmissionRefused) as exc_info:
        submitter.submit_entry(
            result.token,
            _request(gi, result.quantity),
            account=gi.account,
            leg_quotes=gi.leg_quotes,
            quote_snapshot_ids=(uuid.uuid4(),),  # different snapshot
        )
    assert exc_info.value.reason == "quote_snapshot_mismatch"


def test_expired_token_is_refused(conn: psycopg.Connection[Any]) -> None:
    panel = KillSwitchPanel()
    gi, result = _approved(conn, panel)
    assert result.token is not None
    submitter, _ = _submitter(conn, panel, at=NOW + timedelta(seconds=31))
    with pytest.raises(SubmissionRefused) as exc_info:
        submitter.submit_entry(
            result.token,
            _request(gi, result.quantity),
            account=gi.account,
            leg_quotes=gi.leg_quotes,
            quote_snapshot_ids=gi.quote_snapshot_ids,
        )
    assert exc_info.value.reason == "token_expired"


def test_tokens_are_single_use(conn: psycopg.Connection[Any]) -> None:
    panel = KillSwitchPanel()
    gi, result = _approved(conn, panel)
    assert result.token is not None
    submitter, _ = _submitter(conn, panel)
    submitter.submit_entry(
        result.token,
        _request(gi, result.quantity),
        account=gi.account,
        leg_quotes=gi.leg_quotes,
        quote_snapshot_ids=gi.quote_snapshot_ids,
    )
    with pytest.raises(SubmissionRefused) as exc_info:
        submitter.submit_entry(
            result.token,
            _request(gi, result.quantity, attempt=2),
            account=gi.account,
            leg_quotes=gi.leg_quotes,
            quote_snapshot_ids=gi.quote_snapshot_ids,
        )
    assert exc_info.value.reason == "token_already_used"


def test_duplicate_idempotency_key_cannot_create_two_orders(
    conn: psycopg.Connection[Any],
) -> None:
    panel = KillSwitchPanel()
    gi, result = _approved(conn, panel)
    assert result.token is not None
    submitter, machine = _submitter(conn, panel)
    submitter.submit_entry(
        result.token,
        _request(gi, result.quantity),
        account=gi.account,
        leg_quotes=gi.leg_quotes,
        quote_snapshot_ids=gi.quote_snapshot_ids,
    )
    # A fresh token for the SAME proposal cannot reuse the same attempt key.
    result2 = TradeGate(panel=panel, conn=conn, clock=lambda: NOW).evaluate(gi)
    assert result2.approved and result2.token is not None
    with pytest.raises(DuplicateOrderError):
        submitter.submit_entry(
            result2.token,
            _request(gi, result2.quantity),  # same proposal, same attempt=1 key
            account=gi.account,
            leg_quotes=gi.leg_quotes,
            quote_snapshot_ids=gi.quote_snapshot_ids,
        )
    count = conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()
    assert count is not None and count["n"] == 1


def test_mismatched_price_or_quantity_is_refused(conn: psycopg.Connection[Any]) -> None:
    panel = KillSwitchPanel()
    gi, result = _approved(conn, panel)
    assert result.token is not None
    submitter, _ = _submitter(conn, panel)
    with pytest.raises(SubmissionRefused) as exc_info:
        submitter.submit_entry(
            result.token,
            _request(gi, result.quantity + 1),  # one more contract than approved
            account=gi.account,
            leg_quotes=gi.leg_quotes,
            quote_snapshot_ids=gi.quote_snapshot_ids,
        )
    assert exc_info.value.reason == "quantity_mismatch"


def test_something_that_is_not_a_token_is_refused(conn: psycopg.Connection[Any]) -> None:
    panel = KillSwitchPanel()
    gi, result = _approved(conn, panel)
    submitter, _ = _submitter(conn, panel)
    with pytest.raises(SubmissionRefused) as exc_info:
        submitter.submit_entry(
            "definitely-not-a-token",  # type: ignore[arg-type]
            _request(gi, result.quantity),
            account=gi.account,
            leg_quotes=gi.leg_quotes,
            quote_snapshot_ids=gi.quote_snapshot_ids,
        )
    assert exc_info.value.reason == "no_approval_token"


def test_only_the_submitter_calls_broker_submit() -> None:
    """Structurally, the token-enforcing submitter is the one src call site of
    ``.submit_order(`` — there is no token-free path to a broker submit."""
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if path.relative_to(SRC).as_posix() != "execution/submission.py"
        and ".submit_order(" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
