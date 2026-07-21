"""Agent 6 — Execution (Robinhood Agentic Trading MCP client).

Code-first, no LLM (pure API orchestration). Two hard safety properties:

1. **The system stages, it never confirms.** In live mode the execution agent stages and
   submits an order to Robinhood and STOPS — the human approves inside the Robinhood app via
   its native manual-approval + push notification (Section 7.8). The live broker
   (``RobinhoodMCPBroker``) has NO fill/confirm method at all, so there is structurally no
   code path that can confirm a live order.
2. **Paper is fully simulated and isolated.** While ``PAPER_TRADING = True`` the
   ``PaperBroker`` simulates the whole approve+fill lifecycle (tagged ``is_paper=True``) so
   the pipeline is testable end-to-end without touching a real account.

Every purchase re-runs the Section 1.1 settled-cash coverage backstop before staging — a
blocked entry is cheap; a good-faith violation is a 90-day restriction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from config.guardrails import ORDER_APPROVAL_MODE, PAPER_TRADING
from core.event_bus import publish_agent_status, publish_signal_flow
from core.logging_setup import get_logger, log_decision
from core.records import Position
from core.util import utcnow_iso
from risk.sizing import Account, assert_purchase_is_covered

_log = get_logger("execution")

AGENT_KEY = "execution"

SIDE_BUY = "buy"
SIDE_SELL = "sell"

STATUS_BLOCKED = "blocked"
STATUS_STAGED = "staged"
STATUS_PENDING_APPROVAL = "submitted_for_approval"
STATUS_FILLED = "filled"


@dataclass
class OrderRequest:
    symbol: str
    side: str
    quantity: float  # shares (fractional allowed)
    notional_usd: float
    estimated_price: float


@dataclass
class Fill:
    price: float
    shares: float
    ts: str


@dataclass
class ExecutionResult:
    status: str
    symbol: str
    side: str
    quantity: float
    notional_usd: float
    estimated_price: float
    order_id: str | None = None
    broker_ref: str | None = None
    fill: Fill | None = None
    reason: str = ""


# --- broker interface + implementations ------------------------------------


class Broker(Protocol):
    """A broker can stage and submit-for-approval. It CANNOT confirm — that is external."""

    name: str

    def stage(self, order: OrderRequest) -> str: ...

    def submit_for_approval(self, order: OrderRequest, broker_ref: str) -> None: ...


class PaperBroker:
    """Fully-simulated broker for paper mode. Fills at the estimated price, deterministically."""

    name = "paper"

    def stage(self, order: OrderRequest) -> str:
        return f"paper-{order.symbol}-{order.side}"

    def submit_for_approval(self, order: OrderRequest, broker_ref: str) -> None:
        # In paper mode there is no real approval surface; the fill is simulated separately.
        return None

    def simulate_fill(self, order: OrderRequest, broker_ref: str, now_iso: str) -> Fill:
        """Simulate an approved fill (paper only). Never used for a live order."""
        return Fill(price=order.estimated_price, shares=order.quantity, ts=now_iso)


class RobinhoodMCPBroker:
    """Live broker against Robinhood's Agentic Trading MCP (agent.robinhood.com/mcp/trading).

    Intentionally has NO confirm/fill method: it can only stage and submit for approval;
    Robinhood holds the order and the human approves it in-app. Requires an explicit MCP
    transport and a dedicated agentic account id — never provided during the build, so this
    class is never exercised live here.
    """

    name = "robinhood_mcp"

    def __init__(self, mcp_transport: object, agentic_account_id: str) -> None:
        if mcp_transport is None or not agentic_account_id:
            raise RuntimeError(
                "RobinhoodMCPBroker requires a live MCP transport and a dedicated agentic "
                "account id; confirm account isolation before any live funding (Section 8.2)."
            )
        self._transport = mcp_transport
        self._account_id = agentic_account_id

    def stage(self, order: OrderRequest) -> str:
        # Would call the MCP 'preview/prepare order' tool. Not reachable in the paper build.
        raise RuntimeError("live MCP staging is disabled in the paper build")

    def submit_for_approval(self, order: OrderRequest, broker_ref: str) -> None:
        # Would submit the previewed order; Robinhood then notifies the human to approve.
        raise RuntimeError("live MCP submission is disabled in the paper build")


# --- execution agent --------------------------------------------------------


class ExecutionAgent:
    def __init__(
        self,
        broker: Broker | None = None,
        db: object | None = None,
        paper: bool | None = None,
    ) -> None:
        self.paper = PAPER_TRADING if paper is None else paper
        self.broker: Broker = broker if broker is not None else PaperBroker()
        self.db = db

    def place_entry(
        self, symbol: str, shares: float, notional_usd: float, estimated_price: float,
        account: Account, now: datetime,
    ) -> ExecutionResult:
        """Stage (and in paper mode fill) a BUY. Blocks any purchase not covered by settled cash."""
        publish_agent_status(AGENT_KEY, "running", f"staging buy {symbol}", active_model=None)

        # Section 1.1 backstop at the very edge of execution.
        cover = assert_purchase_is_covered(notional_usd, account, now)
        if not cover.allowed:
            return self._record_and_return(
                OrderRequest(symbol, SIDE_BUY, shares, notional_usd, estimated_price),
                STATUS_BLOCKED, reason=cover.reason or "would_use_unsettled_funds",
            )

        return self._stage_and_maybe_fill(
            OrderRequest(symbol, SIDE_BUY, shares, notional_usd, estimated_price), now
        )

    def place_exit(
        self, position: Position, current_price: float, now: datetime,
    ) -> ExecutionResult:
        """Stage (and in paper mode fill) a SELL to close a position.

        No settled-cash guard on the sell side: the position was bought with settled cash
        (the Section 1.1 invariant), so selling it can never create a GFV.
        """
        publish_agent_status(AGENT_KEY, "running", f"staging sell {position.symbol}",
                             active_model=None)
        notional = round(position.shares * current_price, 2)
        order = OrderRequest(position.symbol, SIDE_SELL, position.shares, notional, current_price)
        return self._stage_and_maybe_fill(order, now)

    def _stage_and_maybe_fill(self, order: OrderRequest, now: datetime) -> ExecutionResult:
        broker_ref = self.broker.stage(order)
        result = self._record_and_return(order, STATUS_STAGED, broker_ref=broker_ref)

        self.broker.submit_for_approval(order, broker_ref)
        self._update_status(result, STATUS_PENDING_APPROVAL)

        if self.paper and isinstance(self.broker, PaperBroker):
            # Paper only: simulate the approved fill. No live order is ever confirmed in code.
            fill = self.broker.simulate_fill(order, broker_ref, utcnow_iso())
            result.fill = fill
            self._update_status(result, STATUS_FILLED)
            publish_signal_flow("execution", order.symbol,
                                f"paper {order.side} filled @ {fill.price}")
            log_decision(_log, "paper_fill", symbol=order.symbol, side=order.side,
                         price=fill.price, shares=fill.shares)
        else:
            # LIVE: staged + submitted; the human approves on Robinhood. Execution stops here.
            publish_signal_flow("execution", order.symbol,
                                f"live {order.side} awaiting Robinhood approval")
            log_decision(_log, "awaiting_robinhood_approval", symbol=order.symbol,
                         side=order.side, approval_mode=ORDER_APPROVAL_MODE)
        publish_agent_status(AGENT_KEY, "idle", f"{order.symbol} {result.status}",
                             active_model=None)
        return result

    def _record_and_return(
        self, order: OrderRequest, status: str, broker_ref: str | None = None, reason: str = "",
    ) -> ExecutionResult:
        order_id = None
        if self.db is not None:
            order_id = self.db.insert_order(  # type: ignore[attr-defined]
                symbol=order.symbol, side=order.side, quantity=order.quantity,
                notional_usd=order.notional_usd, estimated_price=order.estimated_price,
                status=status, approval_mode=ORDER_APPROVAL_MODE, is_paper=self.paper,
                reason=reason or None, broker_ref=broker_ref,
            )
        return ExecutionResult(
            status=status, symbol=order.symbol, side=order.side, quantity=order.quantity,
            notional_usd=order.notional_usd, estimated_price=order.estimated_price,
            order_id=order_id, broker_ref=broker_ref, reason=reason,
        )

    def _update_status(self, result: ExecutionResult, status: str) -> None:
        result.status = status
        if self.db is not None and result.order_id is not None:
            self.db.update_order_status(result.order_id, status)  # type: ignore[attr-defined]
