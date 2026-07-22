"""Contract tests for the Robinhood MCP adapter against a mock transport.

No live credentials exist in this build; these tests prove the adapter's behavior
is truthful without them: no transport -> BrokerUnavailable, transport failure ->
BrokerUnavailable (never a fabricated success), live submission structurally
blocked before any transport call, spreads rejected without native multi-leg
support, and limit-only payloads.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from src.config import environments
from src.domain.instruments import LegSide, OptionContract, OptionType
from src.domain.orders import OrderState
from src.execution.interface import (
    BrokerUnavailable,
    LimitOrderRequest,
    LiveOrdersDisabled,
    NetIntent,
    OrderLeg,
    StrategyNotSupported,
)
from src.execution.robinhood_mcp import RobinhoodMCPAdapter, map_broker_state

D = Decimal
EXP = date(2026, 8, 7)

FULL_TOOLS: dict[str, dict[str, Any]] = {
    "get_account": {},
    "get_option_chains": {},
    "place_option_limit_order": {"price_increment": "0.01"},
    "place_multi_leg_option_order": {
        "parameters": {"legs": {"type": "array"}, "spread_type": ["debit", "credit"]}
    },
    "preview_option_order": {},
    "cancel_option_order": {},
    "get_option_events": {},
    "get_order_status": {},
}


class MockTransport:
    """Records every call; serves canned responses; can be told to fail."""

    def __init__(
        self,
        tools: dict[str, dict[str, Any]],
        responses: dict[str, dict[str, Any]] | None = None,
        fail: bool = False,
    ) -> None:
        self.tools = tools
        self.responses = responses or {}
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_tools(self) -> Mapping[str, Mapping[str, Any]]:
        if self.fail:
            raise ConnectionError("transport down")
        return self.tools

    def call(self, tool: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        if self.fail:
            raise ConnectionError("transport down")
        self.calls.append((tool, dict(arguments)))
        if tool not in self.responses:
            raise KeyError(f"no canned response for {tool}")
        return self.responses[tool]


def _spread_request() -> LimitOrderRequest:
    return LimitOrderRequest(
        idempotency_key="p1:1",
        underlying="SPY",
        legs=(
            OrderLeg(OptionContract("SPY", EXP, D("600"), OptionType.CALL), LegSide.BUY, 1),
            OrderLeg(OptionContract("SPY", EXP, D("605"), OptionType.CALL), LegSide.SELL, 1),
        ),
        limit_price=D("1.85"),
        net_intent=NetIntent.DEBIT,
        quantity=1,
    )


def test_no_transport_is_broker_unavailable_never_fabricated() -> None:
    with pytest.raises(BrokerUnavailable, match="no MCP transport"):
        RobinhoodMCPAdapter(transport=None, account_id="ACC-1")  # type: ignore[arg-type]


def test_transport_failure_surfaces_as_unavailable() -> None:
    adapter = RobinhoodMCPAdapter(
        transport=MockTransport(FULL_TOOLS, fail=True), account_id="ACC-1"
    )
    with pytest.raises(BrokerUnavailable, match="tool discovery failed"):
        adapter.discover_capabilities()
    with pytest.raises(BrokerUnavailable, match="failed"):
        adapter.account_snapshot()


def test_live_submission_blocked_before_any_transport_call() -> None:
    """ALLOW_LIVE_ORDERS=False + research_only: submit raises with zero calls made."""
    transport = MockTransport(FULL_TOOLS)
    adapter = RobinhoodMCPAdapter(transport=transport, account_id="ACC-1")
    with pytest.raises(LiveOrdersDisabled):
        adapter.submit_order(_spread_request())
    assert transport.calls == []  # nothing ever left the process


def test_live_gate_holds_even_with_runtime_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_LIVE_HUMAN_CONFIRM", "i-confirm-live")
    transport = MockTransport(FULL_TOOLS)
    adapter = RobinhoodMCPAdapter(transport=transport, account_id="ACC-1")
    with pytest.raises(LiveOrdersDisabled):
        adapter.submit_order(_spread_request())
    assert transport.calls == []


def test_spread_rejected_without_native_multi_leg_no_leg_emulation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with the live gate bypassed (test-only), a single-leg account rejects
    spreads outright — the transport sees zero order calls, not two legs."""
    monkeypatch.setattr(environments, "live_orders_permitted", lambda: True)
    single_leg_tools = {k: v for k, v in FULL_TOOLS.items() if k != "place_multi_leg_option_order"}
    transport = MockTransport(single_leg_tools)
    adapter = RobinhoodMCPAdapter(transport=transport, account_id="ACC-1")
    with pytest.raises(StrategyNotSupported, match="never submitted independently"):
        adapter.submit_order(_spread_request())
    assert transport.calls == []


def test_submit_payload_is_limit_only_and_atomic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(environments, "live_orders_permitted", lambda: True)
    transport = MockTransport(
        FULL_TOOLS,
        responses={"place_multi_leg_option_order": {"order_id": "RH-1", "state": "confirmed"}},
    )
    adapter = RobinhoodMCPAdapter(transport=transport, account_id="ACC-1")
    ack = adapter.submit_order(_spread_request())
    assert ack.broker_order_id == "RH-1"
    assert ack.state is OrderState.OPEN
    ((tool, payload),) = transport.calls
    assert tool == "place_multi_leg_option_order"  # one atomic call, both legs inside
    assert payload["order_type"] == "limit"
    assert len(payload["legs"]) == 2
    assert payload["limit_price"] == "1.85"  # money travels as string, never float


def test_missing_order_id_in_ack_is_uncertain_not_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(environments, "live_orders_permitted", lambda: True)
    transport = MockTransport(
        FULL_TOOLS,
        responses={"place_multi_leg_option_order": {"state": "confirmed"}},
    )
    adapter = RobinhoodMCPAdapter(transport=transport, account_id="ACC-1")
    with pytest.raises(BrokerUnavailable, match="no order_id"):
        adapter.submit_order(_spread_request())


def test_unknown_broker_state_maps_to_reconciliation_required() -> None:
    assert map_broker_state("filled") is OrderState.FILLED
    assert map_broker_state("  Cancelled ") is OrderState.CANCELED
    assert map_broker_state("weird_new_state") is None
    transport = MockTransport(
        FULL_TOOLS,
        responses={
            "get_order_status": {
                "state": "weird_new_state",
                "filled_quantity": 0,
                "remaining_quantity": 1,
            }
        },
    )
    adapter = RobinhoodMCPAdapter(transport=transport, account_id="ACC-1")
    status = adapter.order_status("RH-9")
    assert status.state is OrderState.RECONCILIATION_REQUIRED


def test_float_in_broker_payload_rejected() -> None:
    transport = MockTransport(
        FULL_TOOLS,
        responses={
            "get_account": {
                "total_equity": 1000.5,  # float: refuse to guess
                "settled_cash": "400",
                "unsettled_cash": "0",
            }
        },
    )
    adapter = RobinhoodMCPAdapter(transport=transport, account_id="ACC-1")
    with pytest.raises(BrokerUnavailable, match="refusing to guess"):
        adapter.account_snapshot()


def test_account_id_only_ever_hashed_outward() -> None:
    adapter = RobinhoodMCPAdapter(transport=MockTransport(FULL_TOOLS), account_id="ACC-SECRET")
    assert "ACC-SECRET" not in adapter.account_id_hash
    assert len(adapter.account_id_hash) == 64
