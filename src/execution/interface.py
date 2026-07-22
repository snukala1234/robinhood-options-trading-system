"""Typed broker interface (spec Phase D): one contract for paper and live adapters.

Structural rules encoded in the types themselves:

- **Limit orders only.** :class:`LimitOrderRequest` is the only order type that
  exists; there is no market-order request class and no order-type parameter.
- **Multi-leg is atomic or nothing.** A request carries all its legs; adapters must
  submit them as one broker-native order or raise :class:`StrategyNotSupported`.
  No API exists for submitting a subset of a request's legs.
- **Idempotency is mandatory.** Every request carries ``idempotency_key``; adapters
  must treat a repeated key as the same order, never a new one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from src.domain.instruments import LegSide, OptionContract
from src.domain.orders import OrderState
from src.domain.values import (
    DomainValidationError,
    require_positive_int,
    require_positive_money,
    require_utc,
)


class BrokerError(RuntimeError):
    """Base class for broker-layer failures."""


class BrokerUnavailable(BrokerError):
    """Transport missing/failed. The truthful state is 'unknown', never 'ok'."""


class LiveOrdersDisabled(BrokerError):
    """Live submission attempted while ALLOW_LIVE_ORDERS/ORDER_MODE forbid it."""


class StrategyNotSupported(BrokerError):
    """The broker lacks native support; the strategy is rejected, never emulated."""


class DuplicateOrderError(BrokerError):
    """An idempotency key was reused; the original order id is in ``existing_id``."""

    def __init__(self, key: str, existing_id: str) -> None:
        super().__init__(f"idempotency key {key!r} already used by order {existing_id}")
        self.key = key
        self.existing_id = existing_id


class NetIntent(StrEnum):
    DEBIT = "debit"  # limit is the maximum net price paid
    CREDIT = "credit"  # limit is the minimum net price received


@dataclass(frozen=True)
class OrderLeg:
    """One leg of an order: a concrete contract, side, and ratio quantity."""

    contract: OptionContract
    side: LegSide
    quantity: int

    def __post_init__(self) -> None:
        if not isinstance(self.contract, OptionContract):
            raise DomainValidationError("contract must be an OptionContract")
        if not isinstance(self.side, LegSide):
            raise DomainValidationError("side must be a LegSide")
        require_positive_int("quantity", self.quantity)


@dataclass(frozen=True)
class LimitOrderRequest:
    """The one and only order type: a day limit order, single- or multi-leg atomic."""

    idempotency_key: str
    underlying: str
    legs: tuple[OrderLeg, ...]
    limit_price: Decimal  # net price per share of the whole structure
    net_intent: NetIntent
    quantity: int  # number of structure units
    time_in_force: str = "day"

    def __post_init__(self) -> None:
        if not self.idempotency_key or not isinstance(self.idempotency_key, str):
            raise DomainValidationError("idempotency_key must be a non-empty string")
        if not self.legs:
            raise DomainValidationError("order must have at least one leg")
        require_positive_money("limit_price", self.limit_price)
        if not isinstance(self.net_intent, NetIntent):
            raise DomainValidationError("net_intent must be a NetIntent")
        require_positive_int("quantity", self.quantity)
        if self.time_in_force != "day":
            raise DomainValidationError("only day orders are supported")
        expirations = {leg.contract.expiration for leg in self.legs}
        if len(expirations) != 1:
            raise DomainValidationError(
                "all legs of one order must share an expiration (no calendars in v2)"
            )

    @property
    def is_multi_leg(self) -> bool:
        return len(self.legs) > 1


@dataclass(frozen=True)
class OrderAck:
    broker_order_id: str
    state: OrderState
    raw: dict[str, Any]


@dataclass(frozen=True)
class OrderPreview:
    estimated_net_price: Decimal
    estimated_total_cost: Decimal  # dollars incl. multiplier and quantity
    raw: dict[str, Any]


@dataclass(frozen=True)
class BrokerOrderStatus:
    broker_order_id: str
    state: OrderState
    filled_quantity: int
    remaining_quantity: int
    avg_fill_price: Decimal | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class AccountSnapshot:
    account_id_hash: str  # identifiers are hashed everywhere (Section 17)
    total_equity: Decimal
    settled_cash: Decimal
    unsettled_cash: Decimal
    observed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", require_utc("observed_at", self.observed_at))


@dataclass(frozen=True)
class BrokerPosition:
    occ_symbol: str
    quantity: int  # signed: positive long, negative short
    raw: dict[str, Any]


class BrokerInterface(Protocol):
    """The contract both the paper broker and the Robinhood MCP adapter satisfy."""

    def capabilities(self) -> Any:  # BrokerCapabilities; Any avoids an import cycle
        ...

    def account_snapshot(self) -> AccountSnapshot: ...

    def preview_order(self, request: LimitOrderRequest) -> OrderPreview: ...

    def submit_order(self, request: LimitOrderRequest) -> OrderAck: ...

    def cancel_order(self, broker_order_id: str) -> OrderAck: ...

    def order_status(self, broker_order_id: str) -> BrokerOrderStatus: ...

    def open_orders(self) -> tuple[BrokerOrderStatus, ...]: ...

    def positions(self) -> tuple[BrokerPosition, ...]: ...
