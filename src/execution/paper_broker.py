"""Fully functional paper broker (spec Phase D) — same interface as the live adapter.

Deterministic by design: nothing fills until a test or the paper session driver
says so (``fill``, ``reject``, ``expire_day_orders``). Fill prices must respect the
limit; a debit order can never fill above its limit, a credit order never below.

Idempotency is honored at the broker level as defense in depth: re-submitting a
request whose key was already seen returns the original order's ack — it never
creates a second order. (The order state machine rejects duplicates even earlier.)

The paper broker's capabilities are injectable so tests can prove capability
gating: configured single-leg-only, it refuses multi-leg orders exactly like a
restricted live account would — it does NOT fall back to legging in.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from src.domain.orders import OrderState
from src.domain.values import DomainValidationError, require_positive_money, require_utc
from src.execution.capabilities import BrokerCapabilities
from src.execution.interface import (
    AccountSnapshot,
    BrokerError,
    BrokerOrderStatus,
    BrokerPosition,
    LimitOrderRequest,
    NetIntent,
    OrderAck,
    OrderPreview,
    StrategyNotSupported,
)

#: The paper broker simulates a fully capable account by default.
PAPER_CAPABILITIES = BrokerCapabilities(
    account_read=True,
    option_chain_read=True,
    greeks_available=True,
    single_leg_orders=True,
    multi_leg_orders=True,
    debit_spreads=True,
    credit_spreads=True,
    limit_orders=True,
    preview_supported=True,
    cancel_supported=True,
    replace_supported=False,
    assignment_info_available=True,
    order_state_fields=("state", "filled_quantity", "avg_fill_price"),
    price_increment=Decimal("0.01"),
    source_version="paper-broker-v2",
)

_TERMINAL = {
    OrderState.FILLED,
    OrderState.CANCELED,
    OrderState.REJECTED,
    OrderState.EXPIRED,
}


@dataclass
class _PaperOrder:
    request: LimitOrderRequest
    broker_order_id: str
    state: OrderState
    created_at: datetime
    filled_quantity: int = 0
    fill_notional: Decimal = Decimal("0")  # sum of price*qty for avg price

    @property
    def remaining(self) -> int:
        return self.request.quantity - self.filled_quantity

    @property
    def avg_fill_price(self) -> Decimal | None:
        if self.filled_quantity == 0:
            return None
        return self.fill_notional / self.filled_quantity

    def status(self) -> BrokerOrderStatus:
        return BrokerOrderStatus(
            broker_order_id=self.broker_order_id,
            state=self.state,
            filled_quantity=self.filled_quantity,
            remaining_quantity=self.remaining,
            avg_fill_price=self.avg_fill_price,
            raw={"paper": True},
        )


@dataclass
class PaperBroker:
    """In-memory simulated broker implementing :class:`BrokerInterface`."""

    starting_cash: Decimal
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    caps: BrokerCapabilities = PAPER_CAPABILITIES
    _orders: dict[str, _PaperOrder] = field(default_factory=dict)
    _by_key: dict[str, str] = field(default_factory=dict)
    _positions: dict[str, int] = field(default_factory=dict)
    _cash: Decimal = field(init=False)
    _seq: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        require_positive_money("starting_cash", self.starting_cash)
        self._cash = self.starting_cash

    # -- interface: read ---------------------------------------------------

    def capabilities(self) -> BrokerCapabilities:
        return self.caps

    def account_snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(
            account_id_hash="paper-account",
            total_equity=self._cash,  # positions marked elsewhere; cash view only
            settled_cash=self._cash,
            unsettled_cash=Decimal("0"),
            observed_at=self.clock(),
        )

    def order_status(self, broker_order_id: str) -> BrokerOrderStatus:
        order = self._orders.get(broker_order_id)
        if order is None:
            raise BrokerError(f"unknown paper order {broker_order_id}")
        return order.status()

    def open_orders(self) -> tuple[BrokerOrderStatus, ...]:
        return tuple(o.status() for o in self._orders.values() if o.state not in _TERMINAL)

    def positions(self) -> tuple[BrokerPosition, ...]:
        return tuple(
            BrokerPosition(occ_symbol=occ, quantity=qty, raw={"paper": True})
            for occ, qty in sorted(self._positions.items())
            if qty != 0
        )

    # -- interface: orders -------------------------------------------------

    def _validate_against_capabilities(self, request: LimitOrderRequest) -> None:
        if not self.caps.limit_orders:
            raise StrategyNotSupported("account has no limit-order support")
        if request.is_multi_leg and not self.caps.multi_leg_orders:
            raise StrategyNotSupported(
                "no native multi-leg support; refusing to emulate a spread with independent legs"
            )
        if not request.is_multi_leg and not self.caps.single_leg_orders:
            raise StrategyNotSupported("account has no single-leg order support")
        increment = self.caps.price_increment
        if increment is not None and request.limit_price % increment != 0:
            raise DomainValidationError(
                f"limit_price {request.limit_price} violates price increment {increment}"
            )

    def preview_order(self, request: LimitOrderRequest) -> OrderPreview:
        self._validate_against_capabilities(request)
        multiplier = request.legs[0].contract.multiplier
        total = request.limit_price * multiplier * request.quantity
        return OrderPreview(
            estimated_net_price=request.limit_price,
            estimated_total_cost=total,
            raw={"paper": True},
        )

    def submit_order(self, request: LimitOrderRequest) -> OrderAck:
        existing_id = self._by_key.get(request.idempotency_key)
        if existing_id is not None:
            existing = self._orders[existing_id]
            return OrderAck(
                broker_order_id=existing.broker_order_id,
                state=existing.state,
                raw={"paper": True, "idempotent_replay": True},
            )
        self._validate_against_capabilities(request)
        self._seq += 1
        broker_order_id = f"P-{self._seq:06d}"
        order = _PaperOrder(
            request=request,
            broker_order_id=broker_order_id,
            state=OrderState.OPEN,
            created_at=self.clock(),
        )
        self._orders[broker_order_id] = order
        self._by_key[request.idempotency_key] = broker_order_id
        return OrderAck(broker_order_id=broker_order_id, state=OrderState.OPEN, raw={"paper": True})

    def cancel_order(self, broker_order_id: str) -> OrderAck:
        order = self._orders.get(broker_order_id)
        if order is None:
            raise BrokerError(f"unknown paper order {broker_order_id}")
        if order.state in _TERMINAL:
            raise BrokerError(f"order {broker_order_id} is terminal ({order.state})")
        order.state = OrderState.CANCELED
        return OrderAck(broker_order_id=broker_order_id, state=order.state, raw={"paper": True})

    # -- simulation drivers (paper only, not part of BrokerInterface) ------

    def fill(self, broker_order_id: str, quantity: int, price: Decimal) -> BrokerOrderStatus:
        """Fill some or all of an order at ``price`` (must respect the limit)."""
        order = self._orders.get(broker_order_id)
        if order is None:
            raise BrokerError(f"unknown paper order {broker_order_id}")
        if order.state not in {OrderState.OPEN, OrderState.PARTIALLY_FILLED}:
            raise BrokerError(f"cannot fill order in state {order.state}")
        if quantity < 1 or quantity > order.remaining:
            raise DomainValidationError(
                f"fill quantity {quantity} outside remaining {order.remaining}"
            )
        require_positive_money("price", price)
        request = order.request
        if request.net_intent is NetIntent.DEBIT and price > request.limit_price:
            raise DomainValidationError(f"debit fill {price} above limit {request.limit_price}")
        if request.net_intent is NetIntent.CREDIT and price < request.limit_price:
            raise DomainValidationError(f"credit fill {price} below limit {request.limit_price}")

        order.filled_quantity += quantity
        order.fill_notional += price * quantity
        multiplier = request.legs[0].contract.multiplier
        notional = price * multiplier * quantity
        if request.net_intent is NetIntent.DEBIT:
            self._cash -= notional
        else:
            self._cash += notional
        for leg in request.legs:
            occ = leg.contract.occ_symbol()
            signed = leg.quantity * quantity
            if leg.side.value == "sell":
                signed = -signed
            self._positions[occ] = self._positions.get(occ, 0) + signed
        order.state = OrderState.FILLED if order.remaining == 0 else OrderState.PARTIALLY_FILLED
        return order.status()

    def reject(self, broker_order_id: str, reason: str) -> BrokerOrderStatus:
        order = self._orders.get(broker_order_id)
        if order is None:
            raise BrokerError(f"unknown paper order {broker_order_id}")
        if order.state in _TERMINAL:
            raise BrokerError(f"order {broker_order_id} is terminal ({order.state})")
        order.state = OrderState.REJECTED
        status = order.status()
        return BrokerOrderStatus(
            broker_order_id=status.broker_order_id,
            state=status.state,
            filled_quantity=status.filled_quantity,
            remaining_quantity=status.remaining_quantity,
            avg_fill_price=status.avg_fill_price,
            raw={"paper": True, "reject_reason": reason},
        )

    def expire_day_orders(self, now: datetime) -> tuple[str, ...]:
        """Expire non-terminal day orders from a previous session day."""
        now = require_utc("now", now)
        expired: list[str] = []
        for order in self._orders.values():
            if order.state in _TERMINAL:
                continue
            if order.created_at.date() < now.date():
                order.state = OrderState.EXPIRED
                expired.append(order.broker_order_id)
        return tuple(expired)
