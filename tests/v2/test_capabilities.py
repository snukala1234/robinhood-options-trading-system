"""Capability discovery: runtime-only, fail-closed, snapshot-persisted."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg

from src.execution.capabilities import (
    MOST_RESTRICTIVE,
    BrokerCapabilities,
    CapabilitySnapshotRepository,
    discover_from_tools,
    executable_strategies,
    hash_account_id,
)

FULL_TOOLS: dict[str, dict[str, Any]] = {
    "get_account": {},
    "get_option_chains": {},
    "get_option_greeks": {},
    "place_option_limit_order": {"price_increment": "0.01"},
    "place_multi_leg_option_order": {
        "parameters": {"legs": {"type": "array"}, "spread_type": ["debit", "credit"]}
    },
    "preview_option_order": {},
    "cancel_option_order": {},
    "get_option_events": {},
    "get_order_status": {"state_fields": ["state", "filled_quantity"]},
}

SINGLE_LEG_TOOLS: dict[str, dict[str, Any]] = {
    "get_account": {},
    "get_option_chains": {},
    "place_option_limit_order": {},
    "cancel_option_order": {},
}


def test_default_is_most_restrictive() -> None:
    assert BrokerCapabilities() == MOST_RESTRICTIVE
    assert not MOST_RESTRICTIVE.limit_orders
    assert executable_strategies(MOST_RESTRICTIVE) == frozenset()


def test_full_toolset_enables_all_registry_strategies() -> None:
    caps = discover_from_tools(FULL_TOOLS)
    assert caps.multi_leg_orders and caps.debit_spreads and caps.credit_spreads
    assert caps.price_increment == Decimal("0.01")
    assert caps.order_state_fields == ("state", "filled_quantity")
    assert executable_strategies(caps) == {
        "long_call",
        "long_put",
        "bull_call_debit_spread",
        "bear_put_debit_spread",
        "put_credit_spread",
        "call_credit_spread",
    }


def test_single_leg_account_gets_only_long_options() -> None:
    caps = discover_from_tools(SINGLE_LEG_TOOLS)
    assert not caps.multi_leg_orders
    assert executable_strategies(caps) == {"long_call", "long_put"}


def test_unknown_tools_grant_nothing() -> None:
    caps = discover_from_tools({"mystery_tool": {}, "place_market_order": {}})
    assert caps == BrokerCapabilities()
    assert executable_strategies(caps) == frozenset()


def test_multi_leg_tool_without_legs_schema_not_trusted() -> None:
    tools = dict(FULL_TOOLS)
    tools["place_multi_leg_option_order"] = {"parameters": {"note": "opaque"}}
    caps = discover_from_tools(tools)
    assert not caps.multi_leg_orders
    assert executable_strategies(caps) == {"long_call", "long_put"}


def test_no_limit_orders_means_no_strategies_at_all() -> None:
    tools = {"place_multi_leg_option_order": FULL_TOOLS["place_multi_leg_option_order"]}
    caps = discover_from_tools(tools)
    assert executable_strategies(caps) == frozenset()


def test_account_id_is_hashed() -> None:
    h = hash_account_id("ACC-12345")
    assert h != "ACC-12345" and len(h) == 64
    assert hash_account_id("ACC-12345") == h  # deterministic


def test_snapshot_persistence_single_current_row(
    conn: psycopg.Connection[Any],
) -> None:
    repo = CapabilitySnapshotRepository(conn)
    first = repo.record(discover_from_tools(SINGLE_LEG_TOOLS), account_id_hash=hash_account_id("a"))
    second = repo.record(discover_from_tools(FULL_TOOLS), account_id_hash=hash_account_id("a"))
    assert first != second
    current = repo.current()
    assert current is not None and current["id"] == second
    assert current["capabilities"]["multi_leg_orders"] is True
    assert "put_credit_spread" in current["capabilities"]["executable_strategies"]
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM broker_capability_snapshots WHERE is_current"
    ).fetchone()
    assert n is not None and n["n"] == 1
