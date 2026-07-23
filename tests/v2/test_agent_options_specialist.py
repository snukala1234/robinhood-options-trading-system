"""Agent 4 (Options Specialist): richness boundaries, bands only, never orders."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from src.agents.options_specialist import (
    AGENT_KEY,
    PROMPT_VERSION,
    OptionsSpecialistAgent,
    VolatilityFeaturePacket,
    offline_payload,
)
from src.agents.runtime import AgentRuntime
from src.domain.values import DomainValidationError

D = Decimal
NOW = datetime(2026, 7, 22, 14, 0, tzinfo=UTC)


def _packet(**overrides: Any) -> VolatilityFeaturePacket:
    kwargs: dict[str, Any] = {
        "as_of": NOW,
        "underlying": "SPY",
        "iv": D("0.22"),
        "realized_vol": D("0.20"),
        "iv_realized_spread": D("0.02"),
        "term_structure_shape": "contango",
        "put_call_skew": D("0.03"),
        "expected_move_1sd": D("23.50"),
        "earnings_before_expiration": False,
        "snapshot_ids": (uuid.uuid4(),),
    }
    kwargs.update(overrides)
    return VolatilityFeaturePacket(**kwargs)


def test_richness_boundaries_exact() -> None:
    assert offline_payload(_packet(iv_realized_spread=D("0.05")))["premium_richness"] == "rich"
    assert offline_payload(_packet(iv_realized_spread=D("0.049")))["premium_richness"] == "fair"
    assert offline_payload(_packet(iv_realized_spread=D("-0.03")))["premium_richness"] == "cheap"
    assert offline_payload(_packet(iv_realized_spread=D("-0.029")))["premium_richness"] == "fair"


def test_event_risk_ladder() -> None:
    assert offline_payload(_packet(earnings_before_expiration=True))["event_vol_risk"] == "high"
    assert (
        offline_payload(_packet(term_structure_shape="backwardation"))["event_vol_risk"] == "medium"
    )
    assert offline_payload(_packet())["event_vol_risk"] == "low"


def test_delta_band_tightens_when_premium_rich() -> None:
    rich = offline_payload(_packet(iv_realized_spread=D("0.08")))
    assert (rich["recommended_delta_min"], rich["recommended_delta_max"]) == ("0.25", "0.45")
    fair = offline_payload(_packet())
    assert (fair["recommended_delta_min"], fair["recommended_delta_max"]) == ("0.30", "0.55")


def test_dte_band_is_always_policy_target() -> None:
    payload = offline_payload(_packet())
    assert (payload["recommended_dte_min"], payload["recommended_dte_max"]) == (7, 28)


def test_offline_is_deterministic() -> None:
    assert offline_payload(_packet()) == offline_payload(_packet())


def test_packet_rejects_invalid_inputs() -> None:
    with pytest.raises(DomainValidationError):
        _packet(iv=D("0"))
    with pytest.raises(DomainValidationError):
        _packet(realized_vol=D("-0.1"))
    with pytest.raises(DomainValidationError):
        _packet(iv_realized_spread=0.02)  # float
    with pytest.raises(DomainValidationError):
        _packet(term_structure_shape="humped")
    with pytest.raises(DomainValidationError):
        _packet(snapshot_ids=())


def test_assess_structure_end_to_end_logs_decision(
    conn: psycopg.Connection[Any],
) -> None:
    agent = OptionsSpecialistAgent(AgentRuntime(conn))
    corr = uuid.uuid4()
    result = agent.assess_structure(_packet(), correlation_id=corr)
    assert result.output.premium_richness == "fair"
    assert result.output.recommended_dte_min == 7
    row = conn.execute("SELECT * FROM agent_decisions").fetchone()
    assert row is not None
    assert row["agent_name"] == AGENT_KEY
    assert row["prompt_version"] == PROMPT_VERSION
    assert row["correlation_id"] == corr
