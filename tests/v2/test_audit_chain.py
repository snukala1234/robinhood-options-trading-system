"""Audit-trail tamper evidence: hash chain + append-only + HMAC anchors."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg import errors
from psycopg.types.json import Jsonb

from src.config.environments import ConfigurationError
from src.domain.orders import OrderState
from src.execution.order_state_machine import OrderStateMachine
from src.persistence.audit_chain import (
    AUDIT_CHAIN_TABLES,
    chain_head,
    record_anchor,
    verify_all,
    verify_anchors,
    verify_table,
)

KEY = b"test-anchor-key-outside-the-database"


def _emit_system_event(conn: psycopg.Connection[Any], note: str) -> uuid.UUID:
    event_id = uuid.uuid4()
    conn.execute(
        """INSERT INTO system_events
           (id, created_at, severity, component, event_type, correlation_id, payload)
           VALUES (%s, %s, 'info', 'test', 'chain_test', NULL, %s)""",
        (event_id, datetime.now(UTC), Jsonb({"note": note})),
    )
    return event_id


def _emit_agent_decision(conn: psycopg.Connection[Any]) -> uuid.UUID:
    decision_id = uuid.uuid4()
    conn.execute(
        """INSERT INTO agent_decisions
           (id, correlation_id, agent_name, created_at, model_id, prompt_version,
            input_snapshot_ids, output, validation_result, latency_ms, token_usage)
           VALUES (%s, %s, 'test_agent', %s, 'alias-model', 'test/v1', %s, %s, %s, 1, NULL)""",
        (
            decision_id,
            uuid.uuid4(),
            datetime.now(UTC),
            Jsonb([]),
            Jsonb({"ok": True}),
            Jsonb({"valid": True}),
        ),
    )
    return decision_id


def _disable_guard(conn: psycopg.Connection[Any], table: str, trigger: str) -> None:
    conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")


def test_inserts_form_a_linked_chain(conn: psycopg.Connection[Any]) -> None:
    machine = OrderStateMachine(conn)
    order_id = machine.create_order(idempotency_key="chain:1")
    machine.transition(order_id, OrderState.VALIDATED)
    machine.transition(order_id, OrderState.STAGED)

    rows = conn.execute("SELECT * FROM order_events ORDER BY chain_seq").fetchall()
    assert len(rows) == 3
    assert rows[0]["prev_hash"] == "GENESIS"
    assert rows[1]["prev_hash"] == rows[0]["row_hash"]
    assert rows[2]["prev_hash"] == rows[1]["row_hash"]
    assert all(len(str(r["row_hash"])) == 64 for r in rows)

    report = verify_table(conn, "order_events")
    assert report.intact and report.rows == 3


def test_verify_all_covers_the_three_event_tables(conn: psycopg.Connection[Any]) -> None:
    _emit_system_event(conn, "one")
    _emit_agent_decision(conn)
    OrderStateMachine(conn).create_order(idempotency_key="chain:2")
    reports = verify_all(conn)
    assert set(reports) == set(AUDIT_CHAIN_TABLES)
    assert all(r.intact for r in reports.values())
    assert reports["system_events"].rows == 1
    assert reports["agent_decisions"].rows == 1


def test_append_only_triggers_cover_all_three_tables(conn: psycopg.Connection[Any]) -> None:
    _emit_system_event(conn, "sacred")
    _emit_agent_decision(conn)
    OrderStateMachine(conn).create_order(idempotency_key="chain:3")
    for statement in (
        "UPDATE system_events SET severity = 'debug'",
        "UPDATE agent_decisions SET agent_name = 'evil'",
        "DELETE FROM order_events",
    ):
        with pytest.raises(errors.RaiseException, match="append-only"), conn.transaction():
            conn.execute(statement)


def test_mutated_row_is_detected_even_past_the_triggers(
    conn: psycopg.Connection[Any],
) -> None:
    """Chaos: an 'attacker' (or buggy migration) with trigger-level access
    rewrites history. The chain arithmetic still exposes the exact row."""
    ids = [_emit_system_event(conn, f"event-{i}") for i in range(3)]
    assert verify_table(conn, "system_events").intact

    _disable_guard(conn, "system_events", "trg_system_events_append_only")
    conn.execute(
        "UPDATE system_events SET payload = %s WHERE id = %s",
        (Jsonb({"note": "history rewritten"}), ids[1]),
    )
    report = verify_table(conn, "system_events")
    assert not report.intact
    assert len(report.breaks) == 1
    assert "row hash mismatch" in report.breaks[0].detail


def test_deleted_row_is_detected(conn: psycopg.Connection[Any]) -> None:
    ids = [_emit_system_event(conn, f"event-{i}") for i in range(3)]
    _disable_guard(conn, "system_events", "trg_system_events_append_only")
    conn.execute("DELETE FROM system_events WHERE id = %s", (ids[1],))
    report = verify_table(conn, "system_events")
    assert not report.intact
    assert any("linkage broken" in b.detail for b in report.breaks)


def test_anchors_sign_the_head_with_an_external_key(
    conn: psycopg.Connection[Any],
) -> None:
    _emit_system_event(conn, "anchored")
    record_anchor(conn, "system_events", key=KEY)
    assert verify_anchors(conn, "system_events", key=KEY) == ()
    # The wrong key exposes every anchor as unverifiable.
    problems = verify_anchors(conn, "system_events", key=b"some-other-key")
    assert problems and "HMAC invalid" in problems[0]


def test_rewritten_chain_cannot_forge_anchors(conn: psycopg.Connection[Any]) -> None:
    """An attacker who recomputes the whole sha256 chain still fails the HMAC:
    the anchored row's hash no longer matches what was signed."""
    target = _emit_system_event(conn, "original")
    record_anchor(conn, "system_events", key=KEY)
    head = chain_head(conn, "system_events")
    assert head is not None

    _disable_guard(conn, "system_events", "trg_system_events_append_only")
    # Rewrite the anchored row AND its hash as a capable attacker would.
    conn.execute(
        "UPDATE system_events SET payload = %s, row_hash = 'deadbeef' WHERE id = %s",
        (Jsonb({"note": "rewritten"}), target),
    )
    problems = verify_anchors(conn, "system_events", key=KEY)
    assert problems and "hash changed" in problems[0]


def test_truncated_history_is_detected_by_anchors(conn: psycopg.Connection[Any]) -> None:
    target = _emit_system_event(conn, "will vanish")
    record_anchor(conn, "system_events", key=KEY)
    _disable_guard(conn, "system_events", "trg_system_events_append_only")
    conn.execute("DELETE FROM system_events WHERE id = %s", (target,))
    problems = verify_anchors(conn, "system_events", key=KEY)
    assert problems and "missing" in problems[0]


def test_hmac_key_comes_only_from_the_environment(
    conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AUDIT_CHAIN_HMAC_KEY", raising=False)
    _emit_system_event(conn, "x")
    with pytest.raises(ConfigurationError, match="AUDIT_CHAIN_HMAC_KEY"):
        record_anchor(conn, "system_events")
    monkeypatch.setenv("AUDIT_CHAIN_HMAC_KEY", "from-env")
    record_anchor(conn, "system_events")
    assert verify_anchors(conn, "system_events") == ()


def test_unknown_table_is_refused(conn: psycopg.Connection[Any]) -> None:
    with pytest.raises(ValueError):
        verify_table(conn, "orders")  # not an append-only chained table
