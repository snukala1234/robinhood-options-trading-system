"""Broker capability discovery (spec 5.1): inspect the connected MCP tools at
runtime, never assume support.

The default is :data:`MOST_RESTRICTIVE` — everything unsupported. Discovery can
only *add* capabilities based on the tools actually exposed by the connected
account. The mapping from tool names to capabilities lives in
:data:`TOOL_CAPABILITY_MAP` and must be reviewed against the real tool listing on
first connect (see docs/BROKER_CAPABILITIES.md); an unrecognized tool grants
nothing.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from src.config.strategy_registry import supported_strategies
from src.persistence.db import Connection


@dataclass(frozen=True)
class BrokerCapabilities:
    """What the connected broker account can actually do. Defaults: nothing."""

    account_read: bool = False
    option_chain_read: bool = False
    greeks_available: bool = False
    single_leg_orders: bool = False
    multi_leg_orders: bool = False
    debit_spreads: bool = False
    credit_spreads: bool = False
    limit_orders: bool = False
    preview_supported: bool = False
    cancel_supported: bool = False
    replace_supported: bool = False
    assignment_info_available: bool = False
    order_state_fields: tuple[str, ...] = ()
    price_increment: Decimal | None = None
    source_version: str | None = None

    def capability_tokens(self) -> frozenset[str]:
        """Translate to the strategy registry's requirement tokens."""
        tokens: set[str] = set()
        if self.single_leg_orders and self.limit_orders:
            tokens.update({"buy_to_open_call", "buy_to_open_put"})
        if self.multi_leg_orders:
            tokens.add("multi_leg_options")
        if self.multi_leg_orders and self.debit_spreads:
            tokens.add("debit_spread")
        if self.multi_leg_orders and self.credit_spreads:
            tokens.add("credit_spread")
        if self.assignment_info_available:
            tokens.add("assignment_handling")
        return frozenset(tokens)


MOST_RESTRICTIVE = BrokerCapabilities()

#: Tool-name -> capability flags. Reviewed against the real MCP listing on first
#: connect; names not in this map grant nothing. Schema hints refine flags below.
TOOL_CAPABILITY_MAP: dict[str, tuple[str, ...]] = {
    "get_account": ("account_read",),
    "get_balances": ("account_read",),
    "get_option_chains": ("option_chain_read",),
    "get_option_quote": ("option_chain_read",),
    "get_option_greeks": ("greeks_available",),
    "place_option_limit_order": ("single_leg_orders", "limit_orders"),
    "place_multi_leg_option_order": ("multi_leg_orders",),
    "preview_option_order": ("preview_supported",),
    "cancel_option_order": ("cancel_supported",),
    "replace_option_order": ("replace_supported",),
    "get_option_events": ("assignment_info_available",),
    "get_order_status": (),
}


def discover_from_tools(
    tools: Mapping[str, Mapping[str, Any]], *, source_version: str | None = None
) -> BrokerCapabilities:
    """Derive capabilities from an MCP tool listing (name -> schema/description).

    Multi-leg spread support additionally requires the multi-leg tool's schema to
    declare a ``legs`` array and a spread type; a bare tool name is not enough for
    the system to trust atomic spread execution.
    """
    flags: set[str] = set()
    for name in tools:
        flags.update(TOOL_CAPABILITY_MAP.get(name, ()))

    caps = BrokerCapabilities(
        account_read="account_read" in flags,
        option_chain_read="option_chain_read" in flags,
        greeks_available="greeks_available" in flags,
        single_leg_orders="single_leg_orders" in flags,
        multi_leg_orders="multi_leg_orders" in flags,
        limit_orders="limit_orders" in flags,
        preview_supported="preview_supported" in flags,
        cancel_supported="cancel_supported" in flags,
        replace_supported="replace_supported" in flags,
        assignment_info_available="assignment_info_available" in flags,
        source_version=source_version,
    )

    if caps.multi_leg_orders:
        schema = str(tools.get("place_multi_leg_option_order", {}))
        has_legs_array = "legs" in schema
        caps = replace(
            caps,
            multi_leg_orders=has_legs_array,
            debit_spreads=has_legs_array and "debit" in schema,
            credit_spreads=has_legs_array and "credit" in schema,
        )

    status_schema = tools.get("get_order_status")
    if status_schema is not None:
        fields = status_schema.get("state_fields")
        if isinstance(fields, list | tuple):
            caps = replace(caps, order_state_fields=tuple(str(f) for f in fields))

    increment = None
    for tool_schema in tools.values():
        raw = tool_schema.get("price_increment")
        if isinstance(raw, str):
            increment = Decimal(raw)
            break
    if increment is not None:
        caps = replace(caps, price_increment=increment)

    return caps


def executable_strategies(caps: BrokerCapabilities) -> frozenset[str]:
    """Registry strategies this account can actually trade. Limit-order support is
    a precondition for everything (ALLOW_MARKET_ORDERS is False forever)."""
    if not caps.limit_orders:
        return frozenset()
    return supported_strategies(caps.capability_tokens())


def hash_account_id(account_id: str) -> str:
    """Account identifiers never appear raw in logs or the database (Section 17)."""
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CapabilitySnapshotRepository:
    """Writes broker_capability_snapshots rows; exactly one row is current."""

    conn: Connection

    def record(self, caps: BrokerCapabilities, *, account_id_hash: str) -> uuid.UUID:
        snapshot_id = uuid.uuid4()
        payload = {
            "account_read": caps.account_read,
            "option_chain_read": caps.option_chain_read,
            "greeks_available": caps.greeks_available,
            "single_leg_orders": caps.single_leg_orders,
            "multi_leg_orders": caps.multi_leg_orders,
            "debit_spreads": caps.debit_spreads,
            "credit_spreads": caps.credit_spreads,
            "limit_orders": caps.limit_orders,
            "preview_supported": caps.preview_supported,
            "cancel_supported": caps.cancel_supported,
            "replace_supported": caps.replace_supported,
            "assignment_info_available": caps.assignment_info_available,
            "order_state_fields": list(caps.order_state_fields),
            "price_increment": (
                str(caps.price_increment) if caps.price_increment is not None else None
            ),
            "executable_strategies": sorted(executable_strategies(caps)),
        }
        self.conn.execute(
            "UPDATE broker_capability_snapshots SET is_current = FALSE WHERE is_current"
        )
        self.conn.execute(
            """INSERT INTO broker_capability_snapshots
               (id, observed_at, account_id_hash, capabilities, source_version,
                is_current)
               VALUES (%s, %s, %s, %s, %s, TRUE)""",
            (
                snapshot_id,
                datetime.now(UTC),
                account_id_hash,
                Jsonb(payload),
                caps.source_version,
            ),
        )
        return snapshot_id

    def current(self) -> dict[str, Any] | None:
        cur = self.conn.execute(
            "SELECT * FROM broker_capability_snapshots WHERE is_current LIMIT 1"
        )
        return cur.fetchone()
