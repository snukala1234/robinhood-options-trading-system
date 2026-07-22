"""Section 16 — single source of truth for V2 model routing and failover.

A concrete model ID appears nowhere else in ``src/`` (test-enforced, same invariant
V1 proved for its own tree). Agents are keyed to *aliases*; aliases resolve to model
IDs through environment variables so currently supported IDs are mapped at setup time
without touching agent code.
"""

from __future__ import annotations

import os

# Alias names (Section 16). Resolution order: environment override, then default.
REASONING_ALIAS = "CLAUDE_REASONING_MODEL"
BALANCED_ALIAS = "CLAUDE_BALANCED_MODEL"

# Currently supported Anthropic model IDs (reviewed at setup; env vars override).
_ALIAS_DEFAULTS: dict[str, str] = {
    REASONING_ALIAS: "claude-fable-5",
    BALANCED_ALIAS: "claude-sonnet-5",
}

# The nine V2 reasoning agents (Section 6), keyed to an alias per Section 16.
AGENT_MODEL_ALIASES: dict[str, str] = {
    "market_regime": REASONING_ALIAS,
    "catalyst_research": REASONING_ALIAS,
    "technical_analyst": BALANCED_ALIAS,
    "options_specialist": REASONING_ALIAS,
    "strategy_selector": REASONING_ALIAS,
    "portfolio_manager": REASONING_ALIAS,
    "risk_officer": REASONING_ALIAS,
    "position_manager": BALANCED_ALIAS,
    "performance_auditor": REASONING_ALIAS,
}

ALL_AGENT_KEYS = tuple(AGENT_MODEL_ALIASES.keys())

# Sustained-unavailability failover, one tier down (pattern retained from V1).
# Keyed by alias so the chain follows env-time remapping automatically.
FAILOVER_ALIAS_CHAIN: dict[str, str] = {
    REASONING_ALIAS: BALANCED_ALIAS,
}

MAX_MODEL_UNAVAILABLE_RETRIES = 3


def resolve_alias(alias: str) -> str:
    """Resolve an alias to a concrete model ID (env override, then default).

    Raises ``KeyError`` for an unknown alias so a typo can never silently pick a
    default model.
    """
    default = _ALIAS_DEFAULTS[alias]
    return os.environ.get(alias, default).strip() or default


def model_for(agent_key: str) -> str:
    """Concrete model ID for an agent key. Raises ``KeyError`` on unknown keys."""
    return resolve_alias(AGENT_MODEL_ALIASES[agent_key])


def model_chain(agent_key: str) -> list[str]:
    """[primary, failover, ...] concrete model IDs for an agent key, cycle-safe."""
    alias = AGENT_MODEL_ALIASES[agent_key]
    aliases = [alias]
    nxt = FAILOVER_ALIAS_CHAIN.get(alias)
    while nxt is not None and nxt not in aliases:
        aliases.append(nxt)
        nxt = FAILOVER_ALIAS_CHAIN.get(nxt)
    chain: list[str] = []
    for a in aliases:
        concrete = resolve_alias(a)
        if concrete not in chain:
            chain.append(concrete)
    return chain
