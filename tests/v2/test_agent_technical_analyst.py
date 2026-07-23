"""Agent 3 (Technical Analyst): exact offline zones, no contract fields."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from src.agents.runtime import AgentRuntime
from src.agents.schemas import TechnicalThesis
from src.agents.technical_analyst import (
    AGENT_KEY,
    PROMPT_VERSION,
    TechnicalAnalystAgent,
    TechnicalFeaturePacket,
    offline_payload,
)
from src.domain.values import DomainValidationError

D = Decimal
NOW = datetime(2026, 7, 22, 14, 0, tzinfo=UTC)


def _packet(**overrides: Any) -> TechnicalFeaturePacket:
    kwargs: dict[str, Any] = {
        "as_of": NOW,
        "underlying": "SPY",
        "close": D("600"),
        "sma_20": D("590"),
        "sma_50": D("575"),
        "atr_14": D("8"),
        "roc_10": D("0.04"),
        "recent_high_20": D("610"),
        "recent_low_20": D("585"),
        "trend": "up",
        "snapshot_ids": (uuid.uuid4(),),
    }
    kwargs.update(overrides)
    return TechnicalFeaturePacket(**kwargs)


def test_bullish_zone_and_invalidation_exact() -> None:
    payload = offline_payload(_packet())
    assert payload["direction"] == "bullish"
    assert payload["entry_zone_low"] == "592"  # close - atr
    assert payload["entry_zone_high"] == "600"
    assert payload["invalidation_level"] == "585"  # recent_low_20
    assert payload["time_horizon_days"] == 14


def test_bearish_mirrors_bullish() -> None:
    payload = offline_payload(_packet(trend="down", roc_10=D("-0.04")))
    assert payload["direction"] == "bearish"
    assert payload["entry_zone_low"] == "600"
    assert payload["entry_zone_high"] == "608"  # close + atr
    assert payload["invalidation_level"] == "610"  # recent_high_20


def test_conflicting_signals_are_neutral() -> None:
    payload = offline_payload(_packet(trend="up", roc_10=D("-0.01")))
    assert payload["direction"] == "neutral"
    assert payload["time_horizon_days"] == 10


def test_degenerate_atr_band_falls_back_to_close() -> None:
    payload = offline_payload(_packet(close=D("5"), atr_14=D("6")))
    assert payload["entry_zone_low"] == "5"  # close - atr would be negative


def test_schema_has_no_contract_fields() -> None:
    fields = set(TechnicalThesis.model_fields)
    assert not fields & {"strike", "expiration", "contract", "strategy", "quantity"}


def test_offline_is_deterministic() -> None:
    assert offline_payload(_packet()) == offline_payload(_packet())


def test_packet_rejects_invalid_inputs() -> None:
    with pytest.raises(DomainValidationError):
        _packet(close=600.0)  # float money
    with pytest.raises(DomainValidationError):
        _packet(trend="upward")
    with pytest.raises(DomainValidationError):
        _packet(atr_14=D("0"))
    with pytest.raises(DomainValidationError):
        _packet(snapshot_ids=())
    with pytest.raises(DomainValidationError):
        _packet(as_of=datetime(2026, 7, 22, 14, 0))


def test_analyze_end_to_end_logs_decision(conn: psycopg.Connection[Any]) -> None:
    agent = TechnicalAnalystAgent(AgentRuntime(conn))
    corr = uuid.uuid4()
    result = agent.analyze(_packet(), correlation_id=corr)
    assert result.output.direction == "bullish"
    assert result.output.invalidation_level == "585"
    row = conn.execute("SELECT * FROM agent_decisions").fetchone()
    assert row is not None
    assert row["agent_name"] == AGENT_KEY
    assert row["prompt_version"] == PROMPT_VERSION
    assert row["correlation_id"] == corr
