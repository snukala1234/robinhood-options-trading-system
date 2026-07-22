"""Environment gating and runtime configuration (Sections 17, 20).

Secrets (the database connection string) come only from the environment, never from
source control. Live-order permission requires two independent conditions
(Section 17): the code-level guardrail ``ALLOW_LIVE_ORDERS`` *and* an explicit
runtime human action — and since the guardrail ships ``False`` for this entire
build, :func:`live_orders_permitted` is structurally ``False`` everywhere.
"""

from __future__ import annotations

import os
from enum import IntEnum
from urllib.parse import urlsplit

from src.config import risk_policy

#: Hosts a database connection may target (Section 17: bind local by default).
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class ConfigurationError(RuntimeError):
    """Raised when required runtime configuration is missing or unsafe."""


class OperatingMode(IntEnum):
    """Section 20 operating modes. The build runs in OFFLINE_RESEARCH/LIVE_RESEARCH."""

    OFFLINE_RESEARCH = 0
    LIVE_RESEARCH = 1
    PAPER_EXECUTION = 2
    APPROVAL_REQUIRED_LIVE = 3
    BOUNDED_AUTONOMY = 4


def database_url() -> str:
    """Return ``DATABASE_URL`` from the environment, enforcing a local host.

    Raises :class:`ConfigurationError` when unset or when the host is not local —
    there is deliberately no override flag; remote databases are out of scope for
    this single-user deployment (Section 17).
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise ConfigurationError(
            "DATABASE_URL is not set. Copy .env.example to .env and configure the "
            "local PostgreSQL connection string."
        )
    host = urlsplit(url).hostname
    if host not in _LOCAL_HOSTS:
        raise ConfigurationError(
            f"DATABASE_URL host {host!r} is not local; only {sorted(_LOCAL_HOSTS)} are permitted."
        )
    return url


def order_mode() -> str:
    """The active order mode. Read from the guardrail module, not the environment.

    An environment variable can *lower* privileges in later phases but can never
    raise them; in Phase B there is nothing to lower from ``research_only``.
    """
    return risk_policy.ORDER_MODE


def live_orders_permitted() -> bool:
    """Two independent conditions (Section 17): code guardrail AND runtime human token.

    Ships permanently ``False``: ``ALLOW_LIVE_ORDERS`` is ``False`` in
    :mod:`src.config.risk_policy`, so no environment value alone can enable live
    orders.
    """
    runtime_confirmation = os.environ.get("TRADING_LIVE_HUMAN_CONFIRM") == "i-confirm-live"
    return bool(risk_policy.ALLOW_LIVE_ORDERS) and runtime_confirmation


def current_mode() -> OperatingMode:
    """Resolve the operating mode; anything live is refused while research-only."""
    if order_mode() == "research_only":
        online = os.environ.get("TRADING_MARKET_DATA", "offline").lower() != "offline"
        return OperatingMode.LIVE_RESEARCH if online else OperatingMode.OFFLINE_RESEARCH
    if risk_policy.PAPER_TRADING:
        return OperatingMode.PAPER_EXECUTION
    # Unreachable in this build (ORDER_MODE is research_only); fail closed anyway.
    raise ConfigurationError("live operating modes require the Section 24 checklist")
