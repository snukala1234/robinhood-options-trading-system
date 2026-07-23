"""Agent runtime: logging, one-repair-then-fail-closed, failover tagging, halts."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from src.agents.runtime import (
    REQUIRED_ENTRY_AGENTS,
    AgentRuntime,
    AllModelsUnavailableError,
    InvalidAgentOutput,
    ModelUnavailableError,
    ProviderResponse,
    TransientModelError,
    agent_unavailability_blocks_entries,
    extract_json,
    failover_blocks_new_entry,
)
from src.agents.schemas import RiskOfficerDecision
from src.config.models import model_chain

VALID = {"decision": "veto", "reasons": ["event risk exceeds thesis"]}
CORR = uuid.uuid4()
SNAP = (uuid.uuid4(),)


class ScriptedProvider:
    """Returns scripted responses/errors in order; records every generate call."""

    kind = "scripted"

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, str]] = []

    def generate(self, model: str, system: str, user: str) -> ProviderResponse:
        self.calls.append((model, user))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return ProviderResponse(text=item, input_tokens=10, output_tokens=5)


def _call(runtime: AgentRuntime, *, offline_payload: dict[str, Any] | None = None) -> Any:
    return runtime.call(
        agent_key="risk_officer",
        prompt_version="risk_officer/v1",
        system_prompt="system",
        user_prompt="user",
        output_schema=RiskOfficerDecision,
        offline_payload=offline_payload if offline_payload is not None else dict(VALID),
        input_snapshot_ids=SNAP,
        correlation_id=CORR,
    )


def _decision_rows(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    return conn.execute("SELECT * FROM agent_decisions ORDER BY created_at").fetchall()


def test_offline_call_validates_and_logs_everything(
    conn: psycopg.Connection[Any],
) -> None:
    result = _call(AgentRuntime(conn))
    assert result.output.decision == "veto"
    assert result.provider == "offline"
    assert not result.decided_under_failover
    assert result.model_id == model_chain("risk_officer")[0]

    (row,) = _decision_rows(conn)
    assert row["agent_name"] == "risk_officer"
    assert row["prompt_version"] == "risk_officer/v1"
    assert row["correlation_id"] == CORR
    assert row["input_snapshot_ids"] == [str(SNAP[0])]
    assert row["output"]["decision"] == "veto"
    assert row["validation_result"]["status"] == "ok"
    assert row["latency_ms"] is not None
    assert row["model_id"] == result.model_id


def test_invalid_offline_payload_fails_closed_and_is_logged(
    conn: psycopg.Connection[Any],
) -> None:
    bad = {"decision": "veto"}  # missing required reasons
    with pytest.raises(InvalidAgentOutput):
        _call(AgentRuntime(conn), offline_payload=bad)
    (row,) = _decision_rows(conn)
    assert row["validation_result"]["status"] == "failed"


def test_live_repair_retry_succeeds_and_is_tagged_repaired(
    conn: psycopg.Connection[Any],
) -> None:
    provider = ScriptedProvider(["not json at all", json.dumps(VALID)])
    result = _call(AgentRuntime(conn, provider=provider))
    assert result.validation_status == "repaired"
    assert len(provider.calls) == 2
    assert "rejected by schema validation" in provider.calls[1][1]
    (row,) = _decision_rows(conn)
    assert row["validation_result"]["status"] == "repaired"
    assert row["token_usage"] == {"input": 20, "output": 10}


def test_exactly_one_repair_then_fail_closed(conn: psycopg.Connection[Any]) -> None:
    provider = ScriptedProvider(
        ['{"decision": "veto"}', '{"decision": "veto", "extra_field": 1, "reasons": ["x"]}']
    )
    with pytest.raises(InvalidAgentOutput, match="after one repair"):
        _call(AgentRuntime(conn, provider=provider))
    assert len(provider.calls) == 2  # never a second repair
    (row,) = _decision_rows(conn)
    assert row["validation_result"]["status"] == "failed"


def test_partial_output_never_proceeds(conn: psycopg.Connection[Any]) -> None:
    """approve_with_reduction without reduction_fraction is incoherent: rejected
    twice, then closed — the caller never sees a coerced object."""
    incoherent = json.dumps({"decision": "approve_with_reduction", "reasons": ["r"]})
    provider = ScriptedProvider([incoherent, incoherent])
    with pytest.raises(InvalidAgentOutput):
        _call(AgentRuntime(conn, provider=provider))


def test_transient_errors_retry_same_model(conn: psycopg.Connection[Any]) -> None:
    provider = ScriptedProvider(
        [TransientModelError("429"), TransientModelError("timeout"), json.dumps(VALID)]
    )
    result = _call(AgentRuntime(conn, provider=provider))
    assert not result.decided_under_failover
    models = [m for m, _ in provider.calls]
    assert len(set(models)) == 1  # same model throughout


def test_sustained_failure_fails_over_and_tags_decision(
    conn: psycopg.Connection[Any],
) -> None:
    chain = model_chain("risk_officer")
    assert len(chain) >= 2, "risk_officer must have a failover model configured"
    provider = ScriptedProvider([ModelUnavailableError("gone"), json.dumps(VALID)])
    result = _call(AgentRuntime(conn, provider=provider))
    assert result.decided_under_failover
    assert result.model_id == chain[1]
    (row,) = _decision_rows(conn)
    assert row["validation_result"]["decided_under_failover"] is True
    # ALLOW_NEW_ENTRY_DURING_FAILOVER=False -> this decision cannot open a position.
    assert failover_blocks_new_entry(result)


def test_all_models_unavailable_raises_logs_and_blocks_entries(
    conn: psycopg.Connection[Any],
) -> None:
    chain = model_chain("risk_officer")
    provider = ScriptedProvider([ModelUnavailableError("gone")] * len(chain))
    with pytest.raises(AllModelsUnavailableError):
        _call(AgentRuntime(conn, provider=provider))
    (row,) = _decision_rows(conn)
    assert row["validation_result"]["status"] == "failed"
    blocked, affected = agent_unavailability_blocks_entries(conn, now=datetime.now(UTC))
    assert blocked and affected == ("risk_officer",)


def test_non_required_agent_unavailability_does_not_block(
    conn: psycopg.Connection[Any],
) -> None:
    assert "position_manager" not in REQUIRED_ENTRY_AGENTS  # exits are pure code
    provider = ScriptedProvider(
        [ModelUnavailableError("gone")] * len(model_chain("position_manager"))
    )
    runtime = AgentRuntime(conn, provider=provider)
    with pytest.raises(AllModelsUnavailableError):
        runtime.call(
            agent_key="position_manager",
            prompt_version="position_manager/v1",
            system_prompt="s",
            user_prompt="u",
            output_schema=RiskOfficerDecision,
            offline_payload=dict(VALID),
            input_snapshot_ids=SNAP,
            correlation_id=CORR,
        )
    blocked, affected = agent_unavailability_blocks_entries(conn, now=datetime.now(UTC))
    assert not blocked and affected == ()


def test_unknown_agent_key_fails_loudly(conn: psycopg.Connection[Any]) -> None:
    runtime = AgentRuntime(conn)
    with pytest.raises(KeyError):
        runtime.call(
            agent_key="not_an_agent",
            prompt_version="x/v1",
            system_prompt="s",
            user_prompt="u",
            output_schema=RiskOfficerDecision,
            offline_payload=dict(VALID),
            input_snapshot_ids=SNAP,
            correlation_id=CORR,
        )


def test_extract_json_variants() -> None:
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('prose {"a": {"b": 2}} more prose') == {"a": {"b": 2}}
    assert extract_json("no json here") is None
    assert extract_json("[1, 2, 3]") is None  # a list is not a decision object
