"""Opportunity-cost engine (spec 5.9): candidates compete for limited risk budget.

Passing thresholds is necessary, not sufficient — a candidate must also be among
the best available uses of remaining risk budget and settled cash. This module is
deterministic ranking and eligibility only; reasoning agents interpret its output
but cannot alter it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from src.analytics.opportunity_score import OpportunityScore
from src.config.risk_policy import (
    MAX_RISK_PER_TRADE_PCT,
    MAX_TOTAL_OPEN_RISK_PCT,
    MIN_EXPECTED_VALUE_AFTER_COSTS,
    MIN_OPPORTUNITY_SCORE,
)
from src.domain.values import (
    DomainValidationError,
    require_money,
    require_non_negative_money,
    require_positive_money,
)

_SIX_DP = Decimal("0.000001")


@dataclass(frozen=True)
class CandidateEconomics:
    """One candidate's economics: what it costs, risks, earns, and consumes."""

    candidate_id: str
    score: OpportunityScore
    max_loss: Decimal  # total dollars at defined maximum loss, > 0
    expected_return_after_costs: Decimal  # dollars; may be negative
    capital_required: Decimal  # settled cash consumed (debit + fees / collateral)
    theta_dollars_daily: Decimal  # negative = paying decay
    correlation_with_portfolio: Decimal  # [0, 1]

    def __post_init__(self) -> None:
        if not self.candidate_id:
            raise DomainValidationError("candidate_id must be non-empty")
        require_positive_money("max_loss", self.max_loss)
        require_money("expected_return_after_costs", self.expected_return_after_costs)
        require_positive_money("capital_required", self.capital_required)
        require_money("theta_dollars_daily", self.theta_dollars_daily)
        corr = require_money("correlation_with_portfolio", self.correlation_with_portfolio)
        if not (Decimal("0") <= corr <= Decimal("1")):
            raise DomainValidationError("correlation_with_portfolio must be in [0, 1]")

    @property
    def return_per_risk(self) -> Decimal:
        return (self.expected_return_after_costs / self.max_loss).quantize(_SIX_DP)

    @property
    def return_per_capital(self) -> Decimal:
        return (self.expected_return_after_costs / self.capital_required).quantize(_SIX_DP)

    @property
    def return_per_theta_dollar(self) -> Decimal | None:
        """Expected return per dollar of daily decay paid; None when not paying decay."""
        if self.theta_dollars_daily >= 0:
            return None
        return (self.expected_return_after_costs / -self.theta_dollars_daily).quantize(_SIX_DP)


@dataclass(frozen=True)
class RankedCandidate:
    candidate: CandidateEconomics
    rank: int  # 1-based among eligible; 0 for ineligible
    eligible: bool
    rejection_reasons: tuple[str, ...]
    replacement_value: Decimal | None  # score points vs. weakest open position


def evaluate(
    candidate: CandidateEconomics,
    *,
    account_equity: Decimal,
    settled_cash: Decimal,
    current_open_risk: Decimal,
    weakest_open_score: Decimal | None = None,
) -> RankedCandidate:
    """Eligibility for one candidate against budget, cash, and quality gates."""
    require_positive_money("account_equity", account_equity)
    require_non_negative_money("settled_cash", settled_cash)
    require_non_negative_money("current_open_risk", current_open_risk)

    reasons: list[str] = []
    per_trade_budget = account_equity * Decimal(str(MAX_RISK_PER_TRADE_PCT))
    portfolio_remaining = account_equity * Decimal(str(MAX_TOTAL_OPEN_RISK_PCT)) - current_open_risk
    if candidate.max_loss > per_trade_budget:
        reasons.append(f"max_loss {candidate.max_loss} exceeds per-trade budget {per_trade_budget}")
    if candidate.max_loss > portfolio_remaining:
        reasons.append(
            f"max_loss {candidate.max_loss} exceeds remaining portfolio risk "
            f"budget {portfolio_remaining}"
        )
    if candidate.capital_required > settled_cash:
        reasons.append(
            f"capital_required {candidate.capital_required} exceeds settled cash {settled_cash}"
        )
    if candidate.score.total < Decimal(str(MIN_OPPORTUNITY_SCORE)):
        reasons.append(f"score {candidate.score.total} below minimum {MIN_OPPORTUNITY_SCORE}")
    if candidate.expected_return_after_costs <= Decimal(str(MIN_EXPECTED_VALUE_AFTER_COSTS)):
        reasons.append("expected value after costs is not positive")

    replacement: Decimal | None = None
    if weakest_open_score is not None:
        replacement = candidate.score.total - weakest_open_score

    return RankedCandidate(
        candidate=candidate,
        rank=0,
        eligible=not reasons,
        rejection_reasons=tuple(reasons),
        replacement_value=replacement,
    )


def rank_candidates(
    candidates: Sequence[CandidateEconomics],
    *,
    account_equity: Decimal,
    settled_cash: Decimal,
    current_open_risk: Decimal,
    weakest_open_score: Decimal | None = None,
) -> tuple[RankedCandidate, ...]:
    """Evaluate all candidates and rank the eligible ones.

    Order: expected return per unit of risk (desc), then total score (desc), then
    smaller capital footprint, then candidate_id for a stable total order. An empty
    eligible set is a valid outcome — cash is a position.
    """
    evaluated = [
        evaluate(
            c,
            account_equity=account_equity,
            settled_cash=settled_cash,
            current_open_risk=current_open_risk,
            weakest_open_score=weakest_open_score,
        )
        for c in candidates
    ]
    eligible = [e for e in evaluated if e.eligible]
    eligible.sort(
        key=lambda e: (
            -e.candidate.return_per_risk,
            -e.candidate.score.total,
            e.candidate.capital_required,
            e.candidate.candidate_id,
        )
    )
    ranked = [
        RankedCandidate(
            candidate=e.candidate,
            rank=i + 1,
            eligible=True,
            rejection_reasons=(),
            replacement_value=e.replacement_value,
        )
        for i, e in enumerate(eligible)
    ]
    ranked.extend(e for e in evaluated if not e.eligible)
    return tuple(ranked)
