"""Position sizing by maximum loss (spec Section 9) — never by premium or notional.

``calculate_contract_quantity`` is the Section 9 function verbatim, with one
addition from audit finding 2: an optional ``risk_fraction`` in (0, 1] that
scales the code-computed budget *downward* when the committee requested a
reduction. Agents can only shrink the budget through this parameter — a
fraction above 1 raises instead of sizing up. A resulting quantity of zero
means no trade.
"""

from __future__ import annotations

from decimal import Decimal

from src.config.risk_policy import (
    MAX_CORRELATED_CLUSTER_RISK_PCT,
    MAX_RISK_PER_TRADE_PCT,
    MAX_TOTAL_OPEN_RISK_PCT,
)
from src.domain.values import (
    DomainValidationError,
    require_money,
    require_non_negative_money,
    require_positive_money,
)

_ONE = Decimal("1")


def calculate_contract_quantity(
    account_equity: Decimal,
    settled_cash: Decimal,
    candidate_max_loss_per_unit: Decimal,
    current_open_risk: Decimal,
    correlated_cluster_risk: Decimal,
    *,
    risk_fraction: Decimal = _ONE,
) -> int:
    """Section 9: quantity = effective risk budget // defined max loss per unit."""
    require_positive_money("account_equity", account_equity)
    require_non_negative_money("settled_cash", settled_cash)
    require_money("candidate_max_loss_per_unit", candidate_max_loss_per_unit)
    require_non_negative_money("current_open_risk", current_open_risk)
    require_non_negative_money("correlated_cluster_risk", correlated_cluster_risk)
    require_money("risk_fraction", risk_fraction)
    if risk_fraction > _ONE:
        raise DomainValidationError(
            f"risk_fraction {risk_fraction} > 1: agents may only reduce the "
            "code-computed risk budget, never increase it"
        )
    if risk_fraction < 0:
        raise DomainValidationError(f"risk_fraction must be >= 0, got {risk_fraction}")
    if risk_fraction == 0:
        return 0

    per_trade_budget = account_equity * Decimal(str(MAX_RISK_PER_TRADE_PCT))
    portfolio_remaining = account_equity * Decimal(str(MAX_TOTAL_OPEN_RISK_PCT)) - current_open_risk
    cluster_remaining = (
        account_equity * Decimal(str(MAX_CORRELATED_CLUSTER_RISK_PCT)) - correlated_cluster_risk
    )
    cash_remaining = settled_cash

    budget = (
        min(per_trade_budget, portfolio_remaining, cluster_remaining, cash_remaining)
        * risk_fraction
    )
    if budget <= 0 or candidate_max_loss_per_unit <= 0:
        return 0
    return max(0, int(budget // candidate_max_loss_per_unit))
