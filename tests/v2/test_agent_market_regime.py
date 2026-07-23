"""Reference agent (Market Regime Strategist): deterministic, validated, logged."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from src.agents.market_regime import (
    AGENT_KEY,
    PROMPT_VERSION,
    MarketRegimeAgent,
    RegimeFeaturePacket,
    build_user_prompt,
    offline_payload,
)
from src.agents.runtime import AgentRuntime
from src.domain.values import DomainValidationError

D = Decimal
NOW = datetime(2026, 7, 22, 14, 0, tzinfo=UTC)


def _packet(**overrides: Any) -> RegimeFeaturePacket:
    kwargs: dict[str, Any] = {
        "as_of": NOW,
        "trend": "up",
        "roc_10": D("0.05"),
        "atr_pct": D("0.015"),
        "realized_vol": D("0.18"),
        "iv_realized_spread": D("0.02"),
        "term_structure_shape": "contango",
        "put_call_skew": D("0.03"),
        "snapshot_ids": (uuid.uuid4(),),
    }
    kwargs.update(overrides)
    return RegimeFeaturePacket(**kwargs)


def test_offline_classification_is_deterministic_and_rule_based() -> None:
    assert offline_payload(_packet())["regime"] == "trending_bullish"
    assert offline_payload(_packet(realized_vol=D("0.45")))["regime"] == "high_volatility_expansion"
    assert (
        offline_payload(_packet(realized_vol=D("0.45"), term_structure_shape="backwardation"))[
            "regime"
        ]
        == "risk_off_dislocated"
    )
    assert (
        offline_payload(_packet(trend="sideways", iv_realized_spread=D("-0.05"), roc_10=D("0")))[
            "regime"
        ]
        == "volatility_compression"
    )
    # Same packet, same answer: fully deterministic.
    assert offline_payload(_packet()) == offline_payload(_packet())


def test_assess_returns_validated_output_and_logs(
    conn: psycopg.Connection[Any],
) -> None:
    agent = MarketRegimeAgent(AgentRuntime(conn))
    corr = uuid.uuid4()
    result = agent.assess(_packet(), correlation_id=corr)
    assert result.output.regime == "trending_bullish"
    assert result.agent_key == AGENT_KEY
    assert result.prompt_version == PROMPT_VERSION
    row = conn.execute("SELECT * FROM agent_decisions").fetchone()
    assert row is not None
    assert row["agent_name"] == AGENT_KEY
    assert row["prompt_version"] == PROMPT_VERSION
    assert row["correlation_id"] == corr


def test_packet_rejects_invalid_inputs() -> None:
    with pytest.raises(DomainValidationError):
        _packet(trend="upward")
    with pytest.raises(DomainValidationError):
        _packet(term_structure_shape="weird")
    with pytest.raises(DomainValidationError):
        _packet(realized_vol=0.18)  # float money rejected
    with pytest.raises(DomainValidationError):
        _packet(snapshot_ids=())
    with pytest.raises(DomainValidationError):
        _packet(as_of=datetime(2026, 7, 22, 14, 0))  # naive datetime


def test_prompt_contains_only_packet_data() -> None:
    prompt = build_user_prompt(_packet())
    assert "trend: up" in prompt
    assert "0.18" in prompt and "contango" in prompt
