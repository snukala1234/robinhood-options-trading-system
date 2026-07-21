"""Shared, typed record structures that flow between components.

These are deliberately plain dataclasses (no behaviour) so the persistence layer,
the agents, and the risk engine all agree on shapes without importing each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Signal directions.
LONG = "long"
SHORT = "short"
FLAT = "flat"
DIRECTIONS = (LONG, SHORT, FLAT)


@dataclass
class MarketSnapshot:
    """A point-in-time market read for one symbol (from the market-data module)."""

    symbol: str
    current_price: float
    atr_14: float
    atr_pct: float
    volume: float
    avg_volume: float
    volume_ratio: float
    as_of: str  # ISO-8601 UTC


@dataclass
class ResearchSignal:
    """Output of one research sub-agent (Agent 2) for one symbol."""

    symbol: str
    source_agent: str  # e.g. "research_technical"
    direction: str  # LONG / SHORT / FLAT
    magnitude: float  # 0..1 strength of the view
    raw_confidence: float  # model's stated confidence, pre-calibration
    calibrated_confidence: float  # after Agent 8's recalibration factor
    reasoning: str
    active_model: str
    decided_under_failover: bool = False


@dataclass
class AggregatedSignal:
    """Output of the Edge Aggregator (Agent 3): the ensemble view for one symbol.

    Carries the market read (``current_price``/``atr_14``) so the pure-code sizing
    function in :mod:`risk.sizing` can consume it directly, matching the Section 1
    pseudocode's ``signal.atr_14`` / ``signal.current_price`` interface.
    """

    symbol: str
    direction: str
    magnitude: float
    calibrated_confidence: float
    current_price: float
    atr_14: float
    market_regime: str
    reasoning: str
    active_model: str
    # {agent_name: {"raw_signal": str, "confidence": float, "weight": float}}
    contributing: dict[str, dict[str, object]] = field(default_factory=dict)
    decided_under_failover: bool = False


@dataclass
class Position:
    """An open position, derived from a ``trade_journal`` row that has no exit yet."""

    trade_id: str
    symbol: str
    entry_price: float
    shares: float
    position_size_usd: float
    entry_ts: str
    stop_loss_pct: float
    take_profit_pct: float
