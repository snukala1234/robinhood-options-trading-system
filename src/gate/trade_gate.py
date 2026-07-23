"""The deterministic trade gate (spec Section 3.1, Phase F).

The gate walks the ten-step guardrail precedence in mandatory order and stops
at the first failure — later steps are recorded ``not_evaluated``, so a
downstream pass can structurally never override an upstream rejection. Before
step 1 it aggregates the committee (a Risk Officer veto terminates the
proposal; combined reductions take the minimum) and sizes the trade with the
Section 9 function; quantity, limit price, and max loss are always the
deterministic values — no agent number is ever trusted for money.

Passing steps 1–9 mints the :class:`ApprovalToken` — the ONLY place in the
system a token can come from. The token carries a module-private mint
capability, so constructing one anywhere else raises at ``__post_init__``. It
is short-lived and bound to the proposal, the account-state snapshot, the
quote snapshot, and the kill-switch halt epoch at issuance (audit finding 1).
Step 10, order submission, belongs to the execution adapter
(:mod:`src.execution.submission`), which re-verifies all of it immediately
before broker submit.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from psycopg.types.json import Jsonb

from src.agents.schemas import PortfolioManagerDecision, RiskOfficerDecision
from src.analytics.liquidity import assess as assess_liquidity
from src.analytics.portfolio_exposure import PortfolioExposure
from src.config import environments, risk_policy
from src.config.risk_policy import GUARDRAIL_PRECEDENCE
from src.config.strategy_registry import spec_for
from src.data.option_chains import ContractQuote, StaleQuoteError
from src.domain.proposals import TradeProposal
from src.domain.values import (
    DomainValidationError,
    require_non_negative_money,
    require_positive_money,
    require_utc,
)
from src.execution.capabilities import BrokerCapabilities, executable_strategies
from src.execution.interface import AccountSnapshot, NetIntent
from src.gate.committee import CommitteeOutcome, aggregate_committee
from src.gate.kill_switches import KillSwitchPanel
from src.persistence.db import Connection
from src.risk.settlement import (
    CashAccountState,
    TradeRejected,
    assert_credit_trade_collateral_covered,
    assert_debit_trade_is_covered,
    required_collateral,
    settled_cash_available,
)
from src.risk.sizing import calculate_contract_quantity

#: Approval tokens are short-lived: quotes go stale in 5s, so half a minute is
#: already generous slack for the staging pipeline.
APPROVAL_TOKEN_TTL_SECONDS = 30

#: An account snapshot older than this cannot support a new entry (step 1).
MAX_ACCOUNT_SNAPSHOT_AGE_SECONDS = 60

_CREDIT_STRATEGIES = frozenset({"put_credit_spread", "call_credit_spread"})


class GateViolation(RuntimeError):
    """An attempt to subvert the gate (e.g. forging an approval token)."""


def net_intent_for(strategy: str) -> NetIntent:
    """Whether a registry strategy is entered for a net debit or net credit."""
    return NetIntent.CREDIT if strategy in _CREDIT_STRATEGIES else NetIntent.DEBIT


def hash_account_state(account: AccountSnapshot) -> str:
    """Deterministic digest of the account-state snapshot a token is bound to."""
    payload = "|".join(
        (
            account.account_id_hash,
            str(account.total_equity),
            str(account.settled_cash),
            str(account.unsettled_cash),
            account.observed_at.isoformat(),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def hash_quote_snapshot(quotes: Sequence[ContractQuote], snapshot_ids: Sequence[uuid.UUID]) -> str:
    """Deterministic digest of the quote snapshot a token is bound to."""
    parts = [str(sid) for sid in snapshot_ids]
    parts.extend(
        f"{q.contract.occ_symbol()}|{q.bid}|{q.ask}|{q.observed_at.isoformat()}" for q in quotes
    )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# The module-private mint capability. Only TradeGate.evaluate touches this;
# a structural test proves no other module in src/ references it.
_MINT = object()


@dataclass(frozen=True)
class ApprovalToken:
    """Proof that a specific proposal passed the full gate against a specific
    account state, quote snapshot, and halt epoch — a few seconds ago."""

    token_id: uuid.UUID
    proposal_id: uuid.UUID
    account_state_hash: str
    quote_snapshot_hash: str
    halt_epoch: int
    issued_at: datetime
    expires_at: datetime
    approved_quantity: int
    limit_price: Decimal
    total_max_loss: Decimal
    correlation_id: uuid.UUID
    _mint: object = field(repr=False)

    def __post_init__(self) -> None:
        if self._mint is not _MINT:
            raise GateViolation(
                "approval tokens can only be minted by the deterministic trade gate"
            )


@dataclass(frozen=True)
class CircuitBreakerInputs:
    """Measured losses (dollars, >= 0) for the step-7 circuit breakers."""

    daily_realized_loss: Decimal
    daily_equity_drawdown: Decimal
    weekly_drawdown: Decimal
    peak_to_trough_drawdown: Decimal

    def __post_init__(self) -> None:
        for name in (
            "daily_realized_loss",
            "daily_equity_drawdown",
            "weekly_drawdown",
            "peak_to_trough_drawdown",
        ):
            require_non_negative_money(name, getattr(self, name))


def breached_circuit_breakers(
    breakers: CircuitBreakerInputs, account_equity: Decimal
) -> tuple[str, ...]:
    """Names of tripped step-7 breakers (same vocabulary the panel accepts)."""
    require_positive_money("account_equity", account_equity)
    checks = (
        (
            "daily_realized_loss_breach",
            breakers.daily_realized_loss,
            risk_policy.MAX_DAILY_REALIZED_LOSS_PCT,
        ),
        (
            "daily_equity_drawdown_breach",
            breakers.daily_equity_drawdown,
            risk_policy.MAX_DAILY_EQUITY_DRAWDOWN_PCT,
        ),
        ("weekly_drawdown_breach", breakers.weekly_drawdown, risk_policy.MAX_WEEKLY_DRAWDOWN_PCT),
        (
            "peak_to_trough_drawdown_breach",
            breakers.peak_to_trough_drawdown,
            risk_policy.MAX_PEAK_TO_TROUGH_DRAWDOWN_PCT,
        ),
    )
    return tuple(name for name, loss, pct in checks if loss > account_equity * Decimal(str(pct)))


@dataclass(frozen=True)
class GateInput:
    """Everything the gate needs, all deterministic, all validated upstream."""

    proposal: TradeProposal
    pm_decision: PortfolioManagerDecision
    ro_decision: RiskOfficerDecision
    decided_under_failover: bool
    account: AccountSnapshot
    cash_state: CashAccountState
    capabilities: BrokerCapabilities
    leg_quotes: tuple[ContractQuote, ...]
    quote_snapshot_ids: tuple[uuid.UUID, ...]
    underlying_data_age_seconds: float
    portfolio: PortfolioExposure
    open_position_count: int
    breakers: CircuitBreakerInputs
    reconciliation_blocked_reasons: tuple[str, ...]
    earnings_before_expiration: bool
    correlation_id: uuid.UUID
    correlated_cluster_risk: Decimal = Decimal("0")
    broker_collateral_requirement: Decimal | None = None
    estimated_fees: Decimal = Decimal("0")
    destination: Literal["paper", "live"] = "paper"

    def __post_init__(self) -> None:
        if not self.leg_quotes:
            raise DomainValidationError("at least one leg quote is required")
        if len(self.leg_quotes) != len(self.proposal.legs):
            raise DomainValidationError(
                f"one quote per proposal leg required: {len(self.proposal.legs)} leg(s), "
                f"{len(self.leg_quotes)} quote(s)"
            )
        if not self.quote_snapshot_ids:
            raise DomainValidationError("quote_snapshot_ids must not be empty")
        if (
            isinstance(self.open_position_count, bool)
            or not isinstance(self.open_position_count, int)
            or self.open_position_count < 0
        ):
            raise DomainValidationError("open_position_count must be a non-negative int")
        if isinstance(self.underlying_data_age_seconds, bool) or not isinstance(
            self.underlying_data_age_seconds, int | float
        ):
            raise DomainValidationError("underlying_data_age_seconds must be a number")
        require_non_negative_money("correlated_cluster_risk", self.correlated_cluster_risk)
        require_non_negative_money("estimated_fees", self.estimated_fees)
        if self.broker_collateral_requirement is not None:
            require_positive_money(
                "broker_collateral_requirement", self.broker_collateral_requirement
            )
        if not isinstance(self.correlation_id, uuid.UUID):
            raise DomainValidationError("correlation_id must be a UUID")
        if self.destination not in ("paper", "live"):
            raise DomainValidationError("destination must be 'paper' or 'live'")


@dataclass(frozen=True)
class StepRecord:
    name: str
    status: Literal["passed", "rejected", "not_evaluated", "delegated"]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class GateResult:
    approved: bool
    token: ApprovalToken | None
    quantity: int
    rejection_step: str | None
    reasons: tuple[str, ...]
    steps: tuple[StepRecord, ...]  # all ten, in Section 3.1 order
    committee: CommitteeOutcome


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class TradeGate:
    """The one component that can approve an entry order for submission."""

    panel: KillSwitchPanel
    conn: Connection | None = None
    clock: Callable[[], datetime] = _utcnow

    def evaluate(self, gi: GateInput) -> GateResult:
        now = require_utc("now", self.clock())

        # Gate boundary (audit finding 2): a veto or a no-entry committee
        # outcome terminates the proposal before any guardrail step runs.
        committee = aggregate_committee(gi.pm_decision, gi.ro_decision)
        if not committee.proceed:
            steps = tuple(StepRecord(name, "not_evaluated", ()) for name in GUARDRAIL_PRECEDENCE)
            result = GateResult(
                approved=False,
                token=None,
                quantity=0,
                rejection_step="committee_aggregation",
                reasons=committee.reasons,
                steps=steps,
                committee=committee,
            )
            self._record(gi, result, now)
            return result

        # Section 9 sizing with the committee's (reduce-only) risk fraction.
        quantity = calculate_contract_quantity(
            account_equity=gi.account.total_equity,
            settled_cash=settled_cash_available(gi.cash_state, now.date()),
            candidate_max_loss_per_unit=gi.proposal.max_loss,
            current_open_risk=gi.portfolio.open_risk,
            correlated_cluster_risk=gi.correlated_cluster_risk,
            risk_fraction=committee.effective_risk_fraction,
        )

        checks: tuple[tuple[str, Callable[[], list[str]]], ...] = (
            ("system_health_and_data_freshness", lambda: self._health(gi, now)),
            ("broker_account_capability", lambda: self._capability(gi)),
            ("settlement_and_buying_power", lambda: self._settlement(gi, quantity, now)),
            ("strategy_permission", lambda: self._strategy_permission(gi)),
            ("per_trade_maximum_loss", lambda: self._per_trade(gi, quantity)),
            ("portfolio_exposure", lambda: self._portfolio(gi, quantity)),
            ("circuit_breakers", lambda: self._circuit_breakers(gi)),
            ("liquidity_and_execution", lambda: self._liquidity(gi, quantity)),
            ("human_approval_policy", lambda: self._human_approval(gi)),
        )

        records: list[StepRecord] = []
        rejection_step: str | None = None
        reasons: tuple[str, ...] = ()
        for name, check in checks:
            if rejection_step is not None:
                # No downstream check can override an upstream rejection:
                # downstream checks are not even evaluated.
                records.append(StepRecord(name, "not_evaluated", ()))
                continue
            failures = tuple(check())
            if failures:
                rejection_step = name
                reasons = failures
                records.append(StepRecord(name, "rejected", failures))
            else:
                records.append(StepRecord(name, "passed", ()))

        if rejection_step is not None:
            records.append(StepRecord("order_submission", "not_evaluated", ()))
            result = GateResult(
                approved=False,
                token=None,
                quantity=quantity,
                rejection_step=rejection_step,
                reasons=reasons,
                steps=tuple(records),
                committee=committee,
            )
            self._record(gi, result, now)
            return result

        token = ApprovalToken(
            token_id=uuid.uuid4(),
            proposal_id=gi.proposal.proposal_id,
            account_state_hash=hash_account_state(gi.account),
            quote_snapshot_hash=hash_quote_snapshot(gi.leg_quotes, gi.quote_snapshot_ids),
            halt_epoch=self.panel.halt_epoch,
            issued_at=now,
            expires_at=now + timedelta(seconds=APPROVAL_TOKEN_TTL_SECONDS),
            approved_quantity=quantity,
            limit_price=gi.proposal.limit_price,
            total_max_loss=gi.proposal.max_loss * quantity,
            correlation_id=gi.correlation_id,
            _mint=_MINT,
        )
        records.append(
            StepRecord(
                "order_submission",
                "delegated",
                ("token issued; submission belongs to the execution adapter",),
            )
        )
        result = GateResult(
            approved=True,
            token=token,
            quantity=quantity,
            rejection_step=None,
            reasons=(),
            steps=tuple(records),
            committee=committee,
        )
        self._record(gi, result, now)
        return result

    # -- step 1: system health and data freshness --------------------------

    def _health(self, gi: GateInput, now: datetime) -> list[str]:
        reasons: list[str] = []
        active = self.panel.blocks_new_entries()
        if active:
            reasons.append(f"kill switch(es) active: {', '.join(active)}")
        reasons.extend(gi.reconciliation_blocked_reasons)
        if gi.decided_under_failover and not risk_policy.ALLOW_NEW_ENTRY_DURING_FAILOVER:
            reasons.append(
                "committee decided under model failover; ALLOW_NEW_ENTRY_DURING_FAILOVER is False"
            )
        account_age = (now - gi.account.observed_at).total_seconds()
        if account_age < 0:
            reasons.append("account snapshot observed in the future (clock skew)")
        elif account_age > MAX_ACCOUNT_SNAPSHOT_AGE_SECONDS:
            reasons.append(
                f"account snapshot is {account_age:.1f}s old "
                f"(limit {MAX_ACCOUNT_SNAPSHOT_AGE_SECONDS}s)"
            )
        if gi.underlying_data_age_seconds < 0:
            reasons.append("underlying data observed in the future (clock skew)")
        elif gi.underlying_data_age_seconds > risk_policy.MAX_UNDERLYING_DATA_AGE_SECONDS:
            reasons.append(
                f"underlying data is {gi.underlying_data_age_seconds:.1f}s old "
                f"(limit {risk_policy.MAX_UNDERLYING_DATA_AGE_SECONDS}s)"
            )
        for quote in gi.leg_quotes:
            try:
                quote.require_fresh(now)
            except StaleQuoteError as exc:
                reasons.append(str(exc))
        return reasons

    # -- step 2: broker/account capability ----------------------------------

    def _capability(self, gi: GateInput) -> list[str]:
        reasons: list[str] = []
        if gi.proposal.strategy not in executable_strategies(gi.capabilities):
            reasons.append(
                f"strategy {gi.proposal.strategy!r} is not executable with the "
                "discovered broker capabilities"
            )
        increment = gi.capabilities.price_increment
        if increment is not None and gi.proposal.limit_price % increment != 0:
            reasons.append(
                f"limit_price {gi.proposal.limit_price} violates broker price increment {increment}"
            )
        return reasons

    # -- step 3: settlement and buying power --------------------------------

    def _settlement(self, gi: GateInput, quantity: int, now: datetime) -> list[str]:
        if quantity < 1:
            return [
                "sized quantity is zero: risk budgets or settled cash leave no room for this trade"
            ]
        reasons: list[str] = []
        multiplier = gi.leg_quotes[0].contract.multiplier
        today = now.date()
        if net_intent_for(gi.proposal.strategy) is NetIntent.DEBIT:
            total_debit = gi.proposal.limit_price * multiplier * quantity + gi.estimated_fees
            try:
                assert_debit_trade_is_covered(total_debit, gi.cash_state, today)
            except TradeRejected as exc:
                reasons.append(str(exc))
        else:
            collateral = required_collateral(
                gi.proposal.max_loss * quantity, gi.broker_collateral_requirement
            )
            try:
                assert_credit_trade_collateral_covered(collateral, gi.cash_state, today)
            except TradeRejected as exc:
                reasons.append(str(exc))
        return reasons

    # -- step 4: strategy permission ----------------------------------------

    def _strategy_permission(self, gi: GateInput) -> list[str]:
        reasons: list[str] = []
        spec = spec_for(gi.proposal.strategy)
        if risk_policy.REQUIRE_DEFINED_MAX_LOSS and not spec.defined_risk:
            reasons.append(f"strategy {spec.name!r} is not defined-risk")
        if not risk_policy.ALLOW_ZERO_DTE and gi.proposal.dte == 0:
            reasons.append("zero-DTE entries are prohibited (ALLOW_ZERO_DTE=False)")
        if gi.proposal.dte < risk_policy.ABSOLUTE_DTE_MIN:
            reasons.append(
                f"dte {gi.proposal.dte} below ABSOLUTE_DTE_MIN {risk_policy.ABSOLUTE_DTE_MIN}"
            )
        if gi.proposal.dte > risk_policy.ABSOLUTE_DTE_MAX:
            reasons.append(
                f"dte {gi.proposal.dte} above ABSOLUTE_DTE_MAX {risk_policy.ABSOLUTE_DTE_MAX}"
            )
        if gi.earnings_before_expiration and not risk_policy.ALLOW_EARNINGS_HOLD:
            reasons.append("earnings before expiration and ALLOW_EARNINGS_HOLD is False")
        return reasons

    # -- step 5: per-trade maximum loss --------------------------------------

    def _per_trade(self, gi: GateInput, quantity: int) -> list[str]:
        reasons: list[str] = []
        equity = gi.account.total_equity
        total_max_loss = gi.proposal.max_loss * quantity
        per_trade_cap = equity * Decimal(str(risk_policy.MAX_RISK_PER_TRADE_PCT))
        if total_max_loss > per_trade_cap:
            reasons.append(f"total max loss {total_max_loss} exceeds per-trade cap {per_trade_cap}")
        multiplier = gi.leg_quotes[0].contract.multiplier
        unit_cost = gi.proposal.limit_price * multiplier
        price_cap = equity * Decimal(str(risk_policy.MAX_CONTRACT_PRICE_PCT_OF_EQUITY))
        if unit_cost > price_cap:
            reasons.append(
                f"contract structure price {unit_cost} exceeds "
                f"MAX_CONTRACT_PRICE_PCT_OF_EQUITY cap {price_cap}"
            )
        return reasons

    # -- step 6: portfolio exposure ------------------------------------------

    def _portfolio(self, gi: GateInput, quantity: int) -> list[str]:
        reasons: list[str] = []
        equity = gi.account.total_equity
        total_max_loss = gi.proposal.max_loss * quantity
        if gi.open_position_count + 1 > risk_policy.MAX_CONCURRENT_POSITIONS:
            reasons.append(
                f"{gi.open_position_count} open positions: entry would exceed "
                f"MAX_CONCURRENT_POSITIONS {risk_policy.MAX_CONCURRENT_POSITIONS}"
            )
        total_cap = equity * Decimal(str(risk_policy.MAX_TOTAL_OPEN_RISK_PCT))
        if gi.portfolio.open_risk + total_max_loss > total_cap:
            reasons.append(
                f"open risk {gi.portfolio.open_risk} + trade {total_max_loss} "
                f"exceeds portfolio cap {total_cap}"
            )
        cluster_cap = equity * Decimal(str(risk_policy.MAX_CORRELATED_CLUSTER_RISK_PCT))
        if gi.correlated_cluster_risk + total_max_loss > cluster_cap:
            reasons.append(
                f"correlated cluster risk {gi.correlated_cluster_risk} + trade "
                f"{total_max_loss} exceeds cluster cap {cluster_cap}"
            )
        underlying_cap = equity * Decimal(str(risk_policy.MAX_SINGLE_UNDERLYING_RISK_PCT))
        existing = gi.portfolio.risk_by_underlying.get(gi.proposal.underlying, Decimal("0"))
        if existing + total_max_loss > underlying_cap:
            reasons.append(
                f"underlying {gi.proposal.underlying} risk {existing} + trade "
                f"{total_max_loss} exceeds single-underlying cap {underlying_cap}"
            )
        for check in gi.portfolio.breached_limits():
            reasons.append(f"pre-existing portfolio limit breach: {check.name}")
        return reasons

    # -- step 7: circuit breakers --------------------------------------------

    def _circuit_breakers(self, gi: GateInput) -> list[str]:
        breaches = breached_circuit_breakers(gi.breakers, gi.account.total_equity)
        for name in breaches:
            # Trip the panel: bumps the halt epoch, so any token minted before
            # this evaluation is invalidated at the adapter too.
            self.panel.activate(name, reason="circuit breaker tripped during gate evaluation")
        return [f"circuit breaker breached: {name}" for name in breaches]

    # -- step 8: liquidity and execution -------------------------------------

    def _liquidity(self, gi: GateInput, quantity: int) -> list[str]:
        reasons: list[str] = []
        for quote in gi.leg_quotes:
            assessment = assess_liquidity(quote, quantity)
            if not assessment.passes:
                occ = quote.contract.occ_symbol()
                reasons.extend(f"{occ}: {failure}" for failure in assessment.failures)
        return reasons

    # -- step 9: human approval policy ---------------------------------------

    def _human_approval(self, gi: GateInput) -> list[str]:
        if gi.destination == "live":
            if not environments.live_orders_permitted():
                return [
                    "live order destination refused: live orders are disabled "
                    "(ALLOW_LIVE_ORDERS=False, ORDER_MODE=research_only)"
                ]
            # Unreachable in this build; even if the guardrail moved, every live
            # order still requires a human approval mechanism that does not exist.
            return [
                "live order destination refused: "
                "REQUIRE_HUMAN_APPROVAL_FOR_EVERY_LIVE_ORDER with no approval "
                "mechanism in this build"
            ]
        return []

    # -- audit trail ----------------------------------------------------------

    def _record(self, gi: GateInput, result: GateResult, now: datetime) -> None:
        if self.conn is None:
            return
        if result.approved:
            status = "approved"
        elif result.committee.veto:
            status = "vetoed"
        else:
            status = f"rejected:{result.rejection_step}"
        proposal_doc = {
            "proposal_id": str(gi.proposal.proposal_id),
            "underlying": gi.proposal.underlying,
            "strategy": gi.proposal.strategy,
            "direction": gi.proposal.direction.value,
            "expiration": gi.proposal.expiration.isoformat(),
            "dte": gi.proposal.dte,
            "limit_price": str(gi.proposal.limit_price),
            "max_loss_per_unit": str(gi.proposal.max_loss),
            "sized_quantity": result.quantity,
            "destination": gi.destination,
            "correlation_id": str(gi.correlation_id),
        }
        portfolio_impact = {
            "open_risk": str(gi.portfolio.open_risk),
            "open_position_count": gi.open_position_count,
            "correlated_cluster_risk": str(gi.correlated_cluster_risk),
            "total_max_loss_if_filled": str(gi.proposal.max_loss * result.quantity),
        }
        risk_decision = {
            "committee": {
                "proceed": result.committee.proceed,
                "veto": result.committee.veto,
                "effective_risk_fraction": str(result.committee.effective_risk_fraction),
                "reasons": list(result.committee.reasons),
            },
            "rejection_step": result.rejection_step,
            "rejection_reasons": list(result.reasons),
            "steps": [
                {"name": s.name, "status": s.status, "reasons": list(s.reasons)}
                for s in result.steps
            ],
            "halt_epoch": self.panel.halt_epoch,
            "token_id": str(result.token.token_id) if result.token is not None else None,
        }
        # One row per proposal, keyed by the proposal's own id (orders reference
        # it by FK). Re-evaluating the same proposal — e.g. after a halt was
        # resolved — records the latest gate decision.
        self.conn.execute(
            """INSERT INTO trade_proposals
               (id, candidate_id, created_at, proposal, portfolio_impact,
                risk_decision, approval_status, config_version_id)
               VALUES (%s, NULL, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET
                   created_at = EXCLUDED.created_at,
                   proposal = EXCLUDED.proposal,
                   portfolio_impact = EXCLUDED.portfolio_impact,
                   risk_decision = EXCLUDED.risk_decision,
                   approval_status = EXCLUDED.approval_status""",
            (
                gi.proposal.proposal_id,
                now,
                Jsonb(proposal_doc),
                Jsonb(portfolio_impact),
                Jsonb(risk_decision),
                status,
                gi.proposal.config_version_id,
            ),
        )
