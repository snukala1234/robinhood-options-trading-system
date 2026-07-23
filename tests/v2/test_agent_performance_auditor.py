"""Agent 9 (Performance Auditor): evidence gates; guardrails structurally untouchable."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from pydantic import ValidationError

from src.agents.performance_auditor import (
    AGENT_KEY,
    PROMPT_VERSION,
    AuditPacket,
    BucketStat,
    PerformanceAuditorAgent,
    offline_payload,
)
from src.agents.runtime import AgentRuntime
from src.agents.schemas import TunableProposal
from src.domain.values import DomainValidationError

D = Decimal
NOW = datetime(2026, 7, 22, 14, 0, tzinfo=UTC)

LOSING_BUCKET = BucketStat(
    dimension="strategy=long_call/dte=7-14",
    sample_size=40,
    win_rate=D("0.30"),
    expectancy_after_costs=D("-12.50"),
    avg_slippage=D("2.10"),
)
WINNING_BUCKET = BucketStat(
    dimension="strategy=bull_call_debit_spread/dte=14-21",
    sample_size=45,
    win_rate=D("0.55"),
    expectancy_after_costs=D("9.80"),
    avg_slippage=D("1.40"),
)
TINY_BUCKET = BucketStat(
    dimension="strategy=long_put/dte=7-14",
    sample_size=4,
    win_rate=D("0.25"),
    expectancy_after_costs=D("-30.00"),
    avg_slippage=D("3.00"),
)


def _packet(**overrides: Any) -> AuditPacket:
    kwargs: dict[str, Any] = {
        "as_of": NOW,
        "buckets": (LOSING_BUCKET, WINNING_BUCKET, TINY_BUCKET),
        "min_sample_size": 30,
        "snapshot_ids": (uuid.uuid4(),),
    }
    kwargs.update(overrides)
    return AuditPacket(**kwargs)


def test_under_sampled_buckets_never_produce_findings_or_proposals() -> None:
    payload = offline_payload(_packet(buckets=(TINY_BUCKET,)))
    assert payload["findings"] == []
    assert payload["proposals"] == []
    assert payload["hold_reason"] == "insufficient sample size in all buckets"


def test_losing_well_sampled_bucket_yields_exactly_one_tunable_proposal() -> None:
    payload = offline_payload(_packet())
    assert len(payload["proposals"]) == 1
    proposal = payload["proposals"][0]
    assert proposal["parameter"] == "profit_target_pct_of_max_gain"
    assert proposal["sample_size"] == 40
    assert payload["hold_reason"] is None
    # The tiny losing bucket contributed nothing despite worse numbers.
    assert all("long_put" not in f for f in payload["findings"])


def test_profitable_buckets_hold_with_explicit_reason() -> None:
    payload = offline_payload(_packet(buckets=(WINNING_BUCKET,)))
    assert payload["proposals"] == []
    assert payload["hold_reason"] == "no bucket meets evidence threshold"
    assert len(payload["findings"]) == 1


def test_guardrail_parameter_is_structurally_unproposable() -> None:
    """Agent 9 can never touch policy: the schema rejects guardrail names."""
    with pytest.raises(ValidationError, match="hard guardrail"):
        TunableProposal(
            parameter="MAX_RISK_PER_TRADE_PCT",
            proposed_value=0.05,
            evidence_summary="x",
            sample_size=100,
        )
    with pytest.raises(ValidationError, match="not a pre-approved tunable"):
        TunableProposal(
            parameter="totally_new_knob",
            proposed_value=1.0,
            evidence_summary="x",
            sample_size=100,
        )


def test_offline_is_deterministic() -> None:
    assert offline_payload(_packet()) == offline_payload(_packet())


def test_packet_rejects_invalid_inputs() -> None:
    with pytest.raises(DomainValidationError):
        BucketStat("", 10, D("0.5"), D("1"), D("1"))
    with pytest.raises(DomainValidationError):
        BucketStat("d", 10, D("1.5"), D("1"), D("1"))  # win rate > 1
    with pytest.raises(DomainValidationError):
        BucketStat("d", 10, D("0.5"), 1.0, D("1"))  # type: ignore[arg-type]
    with pytest.raises(DomainValidationError):
        _packet(min_sample_size=0)
    with pytest.raises(DomainValidationError):
        _packet(snapshot_ids=())


def test_audit_end_to_end_logs_decision(conn: psycopg.Connection[Any]) -> None:
    agent = PerformanceAuditorAgent(AgentRuntime(conn))
    corr = uuid.uuid4()
    result = agent.audit(_packet(), correlation_id=corr)
    assert len(result.output.proposals) == 1
    assert result.output.proposals[0].parameter == "profit_target_pct_of_max_gain"
    row = conn.execute("SELECT * FROM agent_decisions").fetchone()
    assert row is not None
    assert row["agent_name"] == AGENT_KEY
    assert row["prompt_version"] == PROMPT_VERSION
    assert row["correlation_id"] == corr
