"""Paper broker: full lifecycle, limit discipline, idempotent replay, gating."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.domain.instruments import LegSide, OptionContract, OptionType
from src.domain.orders import OrderState
from src.domain.values import DomainValidationError
from src.execution.capabilities import BrokerCapabilities
from src.execution.interface import (
    BrokerError,
    LimitOrderRequest,
    NetIntent,
    OrderLeg,
    StrategyNotSupported,
)
from src.execution.paper_broker import PaperBroker

D = Decimal
NOW = datetime(2026, 7, 22, 15, 0, tzinfo=UTC)
EXP = datetime(2026, 8, 7).date()

LONG_CALL = OptionContract("SPY", EXP, D("600"), OptionType.CALL)
SHORT_CALL = OptionContract("SPY", EXP, D("605"), OptionType.CALL)


def _spread_request(key: str = "prop-1:1", quantity: int = 2) -> LimitOrderRequest:
    return LimitOrderRequest(
        idempotency_key=key,
        underlying="SPY",
        legs=(
            OrderLeg(LONG_CALL, LegSide.BUY, 1),
            OrderLeg(SHORT_CALL, LegSide.SELL, 1),
        ),
        limit_price=D("1.85"),
        net_intent=NetIntent.DEBIT,
        quantity=quantity,
    )


def _broker(cash: str = "1000.00") -> PaperBroker:
    return PaperBroker(starting_cash=D(cash), clock=lambda: NOW)


def test_submit_partial_then_full_fill_lifecycle() -> None:
    broker = _broker()
    ack = broker.submit_order(_spread_request())
    assert ack.state is OrderState.OPEN
    assert ack.broker_order_id == "P-000001"

    partial = broker.fill(ack.broker_order_id, 1, D("1.84"))
    assert partial.state is OrderState.PARTIALLY_FILLED
    assert partial.filled_quantity == 1 and partial.remaining_quantity == 1

    final = broker.fill(ack.broker_order_id, 1, D("1.80"))
    assert final.state is OrderState.FILLED
    assert final.avg_fill_price == D("1.82")  # (1.84 + 1.80) / 2
    # Cash: 1000 - (1.84 + 1.80) x 100 = 636.00
    assert broker.account_snapshot().settled_cash == D("636.00")
    # Positions: +2 long 600C, -2 short 605C.
    positions = {p.occ_symbol: p.quantity for p in broker.positions()}
    assert positions[LONG_CALL.occ_symbol()] == 2
    assert positions[SHORT_CALL.occ_symbol()] == -2


def test_debit_fill_above_limit_impossible() -> None:
    broker = _broker()
    ack = broker.submit_order(_spread_request())
    with pytest.raises(DomainValidationError, match="above limit"):
        broker.fill(ack.broker_order_id, 1, D("1.86"))


def test_credit_fill_below_limit_impossible() -> None:
    broker = _broker()
    request = LimitOrderRequest(
        idempotency_key="credit-1:1",
        underlying="SPY",
        legs=(
            OrderLeg(LONG_CALL, LegSide.SELL, 1),
            OrderLeg(SHORT_CALL, LegSide.BUY, 1),
        ),
        limit_price=D("1.85"),
        net_intent=NetIntent.CREDIT,
        quantity=1,
    )
    ack = broker.submit_order(request)
    with pytest.raises(DomainValidationError, match="below limit"):
        broker.fill(ack.broker_order_id, 1, D("1.80"))
    status = broker.fill(ack.broker_order_id, 1, D("1.90"))
    assert status.state is OrderState.FILLED
    assert broker.account_snapshot().settled_cash == D("1190.00")


def test_idempotent_resubmit_never_creates_second_order() -> None:
    broker = _broker()
    first = broker.submit_order(_spread_request(key="dup-key"))
    second = broker.submit_order(_spread_request(key="dup-key"))
    assert second.broker_order_id == first.broker_order_id
    assert second.raw.get("idempotent_replay") is True
    assert len(broker.open_orders()) == 1


def test_cancel_and_terminal_protection() -> None:
    broker = _broker()
    ack = broker.submit_order(_spread_request())
    canceled = broker.cancel_order(ack.broker_order_id)
    assert canceled.state is OrderState.CANCELED
    with pytest.raises(BrokerError, match="terminal"):
        broker.cancel_order(ack.broker_order_id)
    with pytest.raises(BrokerError, match="cannot fill"):
        broker.fill(ack.broker_order_id, 1, D("1.80"))


def test_day_orders_expire_next_session() -> None:
    broker = _broker()
    ack = broker.submit_order(_spread_request())
    assert broker.expire_day_orders(NOW) == ()  # same day: nothing expires
    expired = broker.expire_day_orders(NOW + timedelta(days=1))
    assert expired == (ack.broker_order_id,)
    assert broker.order_status(ack.broker_order_id).state is OrderState.EXPIRED


def test_single_leg_only_account_rejects_spread_never_legs_in() -> None:
    restricted = BrokerCapabilities(
        account_read=True,
        single_leg_orders=True,
        limit_orders=True,
        cancel_supported=True,
        price_increment=D("0.01"),
    )
    broker = PaperBroker(starting_cash=D("1000"), clock=lambda: NOW, caps=restricted)
    with pytest.raises(StrategyNotSupported, match="refusing to emulate"):
        broker.submit_order(_spread_request())
    # Nothing was created: no whole order, and no partial single legs either.
    assert broker.open_orders() == ()


def test_price_increment_enforced() -> None:
    broker = _broker()
    bad = LimitOrderRequest(
        idempotency_key="inc-1:1",
        underlying="SPY",
        legs=(OrderLeg(LONG_CALL, LegSide.BUY, 1),),
        limit_price=D("1.855"),
        net_intent=NetIntent.DEBIT,
        quantity=1,
    )
    with pytest.raises(DomainValidationError, match="increment"):
        broker.submit_order(bad)


def test_fill_quantity_bounds() -> None:
    broker = _broker()
    ack = broker.submit_order(_spread_request(quantity=2))
    with pytest.raises(DomainValidationError, match="outside remaining"):
        broker.fill(ack.broker_order_id, 3, D("1.80"))
    with pytest.raises(BrokerError, match="unknown"):
        broker.fill("P-999999", 1, D("1.80"))


def test_preview_math() -> None:
    broker = _broker()
    preview = broker.preview_order(_spread_request(quantity=2))
    assert preview.estimated_net_price == D("1.85")
    assert preview.estimated_total_cost == D("370.00")  # 1.85 x 100 x 2
