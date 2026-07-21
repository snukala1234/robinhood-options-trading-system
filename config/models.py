"""Section 3.7 / 3.8 — single source of truth for model routing and failover.

Every LLM-backed agent resolves its model from this module; a model string appears
NOWHERE else in the codebase. Phase 1 is a uniform ``claude-fable-5`` build — each row is
independently swappable later with zero changes to agent logic. ``FAILOVER_CHAIN`` adds the
Section 3.8 automatic (not just manual) failover for model-availability resilience.
"""

from __future__ import annotations

# Uniform Fable 5 build for Phase 1 (simplest path to one working build).
AGENT_MODELS: dict[str, str] = {
    "scanner": "claude-fable-5",
    "research_technical": "claude-fable-5",
    "research_fundamental": "claude-fable-5",
    "research_sentiment": "claude-fable-5",
    "research_macro": "claude-fable-5",
    "edge_aggregator": "claude-fable-5",
    "portfolio_construct": "claude-fable-5",
    "risk_manager_flagging": "claude-fable-5",  # narrative flagging only; core logic is code
    "exit_monitor": "claude-fable-5",
    "auditor_calibration": "claude-fable-5",
}

PRIMARY_MODEL = "claude-fable-5"

# If a model is unreachable, fall back one tier (Section 3.8).
FAILOVER_CHAIN: dict[str, str] = {
    "claude-fable-5": "claude-opus-4-8",
    "claude-opus-4-8": "claude-sonnet-5",
}

MAX_MODEL_UNAVAILABLE_RETRIES = 3

ALL_AGENT_KEYS = tuple(AGENT_MODELS.keys())


def model_for(agent_key: str) -> str:
    """Return the configured model id for an agent key.

    Raises ``KeyError`` for an unknown key so a typo can never silently pick a default.
    """
    return AGENT_MODELS[agent_key]


def failover_model(model: str) -> str | None:
    """Return the next model in the failover chain, or ``None`` if at the end."""
    return FAILOVER_CHAIN.get(model)


def model_chain(agent_key: str) -> list[str]:
    """Return [primary, failover1, failover2, ...] for an agent key, cycle-safe."""
    chain = [model_for(agent_key)]
    current = chain[0]
    nxt = failover_model(current)
    while nxt is not None and nxt not in chain:
        chain.append(nxt)
        nxt = failover_model(nxt)
    return chain
