"""Typed Robinhood MCP adapter behind :class:`BrokerInterface` (spec Phase D).

No live credentials are connected during this build, and this module never
pretends otherwise: it operates only through an injected :class:`MCPTransport`;
without one, construction fails with :class:`BrokerUnavailable`. Transport errors
surface as :class:`BrokerUnavailable` — the truthful answer is "state unknown",
never a fabricated success. See docs/BROKER_CAPABILITIES.md for the exact human
steps to connect the real Trading MCP later.

Structural gates, in order, before any submit-side transport call:

1. ``environments.live_orders_permitted()`` — permanently False in this build
   (``ALLOW_LIVE_ORDERS=False`` + no runtime human token), so ``submit_order``
   raises :class:`LiveOrdersDisabled` before anything leaves the process.
2. Capability validation — multi-leg without native atomic support raises
   :class:`StrategyNotSupported`; legs are never submitted independently.
3. Limit orders only — the payload hardcodes ``order_type="limit"``; no other
   order type can be expressed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from src.config import environments
from src.domain.orders import OrderState
from src.domain.values import DomainValidationError
from src.execution.capabilities import (
    BrokerCapabilities,
    discover_from_tools,
    hash_account_id,
)
from src.execution.interface import (
    AccountSnapshot,
    BrokerOrderStatus,
    BrokerPosition,
    BrokerUnavailable,
    LimitOrderRequest,
    LiveOrdersDisabled,
    OrderAck,
    OrderPreview,
    StrategyNotSupported,
)


class MCPTransport(Protocol):
    """Minimal transport the adapter needs from an MCP client session."""

    def list_tools(self) -> Mapping[str, Mapping[str, Any]]: ...

    def call(self, tool: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]: ...


#: Broker order-state strings -> Section 12.2 states. Anything absent maps to None
#: and the caller must treat the order as RECONCILIATION_REQUIRED, never guess.
BROKER_STATE_MAP: dict[str, OrderState] = {
    "queued": OrderState.SUBMITTED,
    "submitted": OrderState.SUBMITTED,
    "confirmed": OrderState.OPEN,
    "open": OrderState.OPEN,
    "partially_filled": OrderState.PARTIALLY_FILLED,
    "filled": OrderState.FILLED,
    "canceled": OrderState.CANCELED,
    "cancelled": OrderState.CANCELED,
    "rejected": OrderState.REJECTED,
    "expired": OrderState.EXPIRED,
}


def map_broker_state(raw_state: str) -> OrderState | None:
    return BROKER_STATE_MAP.get(raw_state.strip().lower())


def _dec(payload: Mapping[str, Any], key: str) -> Decimal:
    value = payload.get(key)
    if value is None or isinstance(value, float):
        raise BrokerUnavailable(
            f"broker payload field {key!r} missing or float ({value!r}); refusing to guess"
        )
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise BrokerUnavailable(f"broker payload field {key!r} not numeric: {value!r}") from exc


@dataclass
class RobinhoodMCPAdapter:
    """BrokerInterface implementation over the Robinhood Trading MCP."""

    transport: MCPTransport
    account_id: str
    _caps: BrokerCapabilities | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.transport is None:  # explicit: a None transport is a config error
            raise BrokerUnavailable(
                "no MCP transport connected; see docs/BROKER_CAPABILITIES.md for setup"
            )
        if not self.account_id:
            raise BrokerUnavailable("account_id required (it is stored only as a hash)")

    # -- capability discovery ---------------------------------------------

    def discover_capabilities(self) -> BrokerCapabilities:
        """Inspect the connected MCP tool listing; cache for this session."""
        try:
            tools = self.transport.list_tools()
        except Exception as exc:
            raise BrokerUnavailable(f"tool discovery failed: {exc}") from exc
        self._caps = discover_from_tools(tools, source_version="robinhood-mcp")
        return self._caps

    def capabilities(self) -> BrokerCapabilities:
        if self._caps is None:
            return self.discover_capabilities()
        return self._caps

    @property
    def account_id_hash(self) -> str:
        return hash_account_id(self.account_id)

    # -- reads -------------------------------------------------------------

    def _call(self, tool: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            return self.transport.call(tool, arguments)
        except Exception as exc:
            raise BrokerUnavailable(f"MCP call {tool!r} failed: {exc}") from exc

    def account_snapshot(self) -> AccountSnapshot:
        payload = self._call("get_account", {"account_id": self.account_id})
        return AccountSnapshot(
            account_id_hash=self.account_id_hash,
            total_equity=_dec(payload, "total_equity"),
            settled_cash=_dec(payload, "settled_cash"),
            unsettled_cash=_dec(payload, "unsettled_cash"),
            observed_at=datetime.now(UTC),
        )

    def order_status(self, broker_order_id: str) -> BrokerOrderStatus:
        payload = self._call("get_order_status", {"order_id": broker_order_id})
        raw_state = str(payload.get("state", ""))
        state = map_broker_state(raw_state)
        if state is None:
            state = OrderState.RECONCILIATION_REQUIRED
        filled = int(payload.get("filled_quantity", 0))
        remaining = int(payload.get("remaining_quantity", 0))
        avg_raw = payload.get("avg_fill_price")
        avg = _dec(payload, "avg_fill_price") if avg_raw is not None else None
        return BrokerOrderStatus(
            broker_order_id=broker_order_id,
            state=state,
            filled_quantity=filled,
            remaining_quantity=remaining,
            avg_fill_price=avg,
            raw=dict(payload),
        )

    def open_orders(self) -> tuple[BrokerOrderStatus, ...]:
        payload = self._call("get_open_orders", {"account_id": self.account_id})
        orders = payload.get("orders")
        if not isinstance(orders, list):
            raise BrokerUnavailable("get_open_orders returned no order list")
        return tuple(self.order_status(str(o.get("order_id"))) for o in orders)

    def positions(self) -> tuple[BrokerPosition, ...]:
        payload = self._call("get_positions", {"account_id": self.account_id})
        rows = payload.get("positions")
        if not isinstance(rows, list):
            raise BrokerUnavailable("get_positions returned no position list")
        return tuple(
            BrokerPosition(
                occ_symbol=str(r["occ_symbol"]),
                quantity=int(r["quantity"]),
                raw=dict(r),
            )
            for r in rows
        )

    # -- orders ------------------------------------------------------------

    def _validate_order(self, request: LimitOrderRequest) -> None:
        caps = self.capabilities()
        if not caps.limit_orders:
            raise StrategyNotSupported("connected account has no limit-order support")
        if request.is_multi_leg and not caps.multi_leg_orders:
            raise StrategyNotSupported(
                "no native multi-leg support on this account; the strategy is "
                "rejected — legs are never submitted independently"
            )
        if not request.is_multi_leg and not caps.single_leg_orders:
            raise StrategyNotSupported("no single-leg order support on this account")
        increment = caps.price_increment
        if increment is not None and request.limit_price % increment != 0:
            raise DomainValidationError(
                f"limit_price {request.limit_price} violates increment {increment}"
            )

    def _order_payload(self, request: LimitOrderRequest) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "order_type": "limit",  # the only order type this system can express
            "time_in_force": request.time_in_force,
            "idempotency_key": request.idempotency_key,
            "underlying": request.underlying,
            "net_intent": request.net_intent.value,
            "limit_price": str(request.limit_price),
            "quantity": request.quantity,
            "legs": [
                {
                    "occ_symbol": leg.contract.occ_symbol(),
                    "side": leg.side.value,
                    "quantity": leg.quantity,
                }
                for leg in request.legs
            ],
        }

    def preview_order(self, request: LimitOrderRequest) -> OrderPreview:
        self._validate_order(request)
        caps = self.capabilities()
        if not caps.preview_supported:
            raise StrategyNotSupported("preview not supported by connected account")
        payload = self._call("preview_option_order", self._order_payload(request))
        return OrderPreview(
            estimated_net_price=_dec(payload, "estimated_net_price"),
            estimated_total_cost=_dec(payload, "estimated_total_cost"),
            raw=dict(payload),
        )

    def submit_order(self, request: LimitOrderRequest) -> OrderAck:
        # Gate 1: structural. False for this entire build — no transport call is
        # ever made from here while ALLOW_LIVE_ORDERS is False.
        if not environments.live_orders_permitted():
            raise LiveOrdersDisabled(
                "live orders are disabled (ALLOW_LIVE_ORDERS=False / research_only)"
            )
        # Gate 2: capabilities (multi-leg atomic or reject; never leg in).
        self._validate_order(request)
        tool = (
            "place_multi_leg_option_order" if request.is_multi_leg else "place_option_limit_order"
        )
        payload = self._call(tool, self._order_payload(request))
        raw_state = str(payload.get("state", ""))
        state = map_broker_state(raw_state) or OrderState.RECONCILIATION_REQUIRED
        order_id = payload.get("order_id")
        if not order_id:
            raise BrokerUnavailable("broker returned no order_id; state uncertain")
        return OrderAck(broker_order_id=str(order_id), state=state, raw=dict(payload))

    def cancel_order(self, broker_order_id: str) -> OrderAck:
        caps = self.capabilities()
        if not caps.cancel_supported:
            raise StrategyNotSupported("cancel not supported by connected account")
        payload = self._call("cancel_option_order", {"order_id": broker_order_id})
        raw_state = str(payload.get("state", ""))
        state = map_broker_state(raw_state) or OrderState.RECONCILIATION_REQUIRED
        return OrderAck(broker_order_id=broker_order_id, state=state, raw=dict(payload))
