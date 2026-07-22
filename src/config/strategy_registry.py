"""Section 7.1 — supported strategy registry.

The registry is the single place strategy assumptions live. At runtime it is
intersected with the discovered broker capabilities and account permissions
(Phase D); unsupported structures disappear from the candidate universe before
research spends tokens on them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategySpec:
    """One permitted strategy structure and the broker capabilities it requires."""

    name: str
    defined_risk: bool
    legs: int
    requires: tuple[str, ...]


STRATEGY_REGISTRY: dict[str, StrategySpec] = {
    "long_call": StrategySpec(
        name="long_call",
        defined_risk=True,
        legs=1,
        requires=("buy_to_open_call",),
    ),
    "long_put": StrategySpec(
        name="long_put",
        defined_risk=True,
        legs=1,
        requires=("buy_to_open_put",),
    ),
    "bull_call_debit_spread": StrategySpec(
        name="bull_call_debit_spread",
        defined_risk=True,
        legs=2,
        requires=("multi_leg_options", "debit_spread"),
    ),
    "bear_put_debit_spread": StrategySpec(
        name="bear_put_debit_spread",
        defined_risk=True,
        legs=2,
        requires=("multi_leg_options", "debit_spread"),
    ),
    "put_credit_spread": StrategySpec(
        name="put_credit_spread",
        defined_risk=True,
        legs=2,
        requires=("multi_leg_options", "credit_spread", "assignment_handling"),
    ),
    "call_credit_spread": StrategySpec(
        name="call_credit_spread",
        defined_risk=True,
        legs=2,
        requires=("multi_leg_options", "credit_spread", "assignment_handling"),
    ),
}


def spec_for(strategy: str) -> StrategySpec:
    """Registry lookup. Raises ``KeyError`` — an unknown strategy is never guessed."""
    return STRATEGY_REGISTRY[strategy]


def supported_strategies(broker_capabilities: frozenset[str]) -> frozenset[str]:
    """Strategies whose every requirement is present in the capability snapshot."""
    return frozenset(
        name
        for name, spec in STRATEGY_REGISTRY.items()
        if all(req in broker_capabilities for req in spec.requires)
    )
