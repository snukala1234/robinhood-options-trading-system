"""Committee aggregation at the gate boundary (audit finding 2).

The reasoning committee's Portfolio Manager and Independent Risk Officer
decisions are aggregated here, in code, before the gate walks its ten steps:

- A Risk Officer **veto terminates the proposal** — the gate never evaluates
  further and no approval token can exist for it.
- When both agents request reductions, the effective risk fraction is the
  **minimum** of the two. It scales the code-computed risk budget fed into the
  deterministic sizing function — agents may only *reduce* the cap, never
  increase it (fractions above 1 are unrepresentable in the schemas and
  rejected again by the sizing function as defense in depth).
- ``limit_price`` and ``max_loss`` are always the deterministic analytics
  values carried on the proposal; nothing in this module reads a price or a
  loss number from an agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.agents.schemas import PortfolioManagerDecision, RiskOfficerDecision

_ONE = Decimal("1")


@dataclass(frozen=True)
class CommitteeOutcome:
    """The aggregated committee verdict the gate consumes."""

    proceed: bool
    veto: bool
    effective_risk_fraction: Decimal  # in (0, 1]; meaningful only when proceed
    reasons: tuple[str, ...]


def aggregate_committee(pm: PortfolioManagerDecision, ro: RiskOfficerDecision) -> CommitteeOutcome:
    """Aggregate the two capital-allocation decisions into one gate input."""
    if ro.decision == "veto":
        return CommitteeOutcome(
            proceed=False,
            veto=True,
            effective_risk_fraction=Decimal("0"),
            reasons=("risk_officer_veto", *ro.reasons),
        )
    if pm.action in ("hold_cash", "defer"):
        return CommitteeOutcome(
            proceed=False,
            veto=False,
            effective_risk_fraction=Decimal("0"),
            reasons=(f"portfolio_manager_{pm.action}",),
        )

    pm_fraction = Decimal(pm.risk_fraction_of_request)
    ro_fraction = (
        Decimal(ro.reduction_fraction)
        if ro.decision == "approve_with_reduction" and ro.reduction_fraction is not None
        else _ONE
    )
    # The smaller reduction always wins, and nothing can exceed the code cap.
    effective = min(pm_fraction, ro_fraction, _ONE)
    if effective <= 0:
        return CommitteeOutcome(
            proceed=False,
            veto=False,
            effective_risk_fraction=Decimal("0"),
            reasons=("effective_risk_fraction_zero",),
        )

    reasons: list[str] = [f"portfolio_manager_{pm.action}", f"risk_officer_{ro.decision}"]
    if effective < _ONE:
        reasons.append(f"risk_reduced_to_{effective}")
    return CommitteeOutcome(
        proceed=True,
        veto=False,
        effective_risk_fraction=effective,
        reasons=tuple(reasons),
    )
