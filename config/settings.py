"""Runtime settings that are operational (not capital-at-risk guardrails).

Capital-at-risk constants live in ``config.guardrails`` and must never be set here.
This module only holds operational knobs: where the database lives, the default
symbol universe, the phase table (Section 5), and timezone.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Repository root (this file is <root>/config/settings.py).
REPO_ROOT = Path(__file__).resolve().parent.parent

# SQLite database path. Overridable via env for tests / alternate deployments.
# Spec permits Postgres/SQLite; SQLite chosen for local single-user Phase 1.
DEFAULT_DB_PATH = REPO_ROOT / "data" / "trading.db"
DB_PATH = Path(os.environ.get("TRADING_DB_PATH", str(DEFAULT_DB_PATH)))

# Timezone used for session/day rollups. US equities trade on US/Eastern.
MARKET_TIMEZONE = "America/New_York"

# Default Phase-1 candidate universe for the scanner (liquid large caps).
# The scanner filters this down; it is not a set of positions.
DEFAULT_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL",
    "META", "TSLA", "AMD", "NFLX", "JPM",
]


@dataclass(frozen=True)
class PhaseConfig:
    """One row of the Section 5 phased scaling plan."""

    phase: int
    equity_min: float
    equity_max: float | None
    max_positions: int
    max_position_pct: float
    approval_mode: str


# Section 5 — Phased scaling plan. Phase 1 is active for this build.
PHASES: dict[int, PhaseConfig] = {
    1: PhaseConfig(1, 100.0, 1_000.0, 3, 0.40, "manual_every_order"),
    2: PhaseConfig(2, 1_000.0, 5_000.0, 6, 0.25, "manual_below_threshold_auto_above"),
    3: PhaseConfig(3, 5_000.0, 25_000.0, 10, 0.15, "auto_with_daily_review"),
    4: PhaseConfig(4, 25_000.0, None, 999, 0.10, "auto_with_circuit_breakers"),
}

# The build targets Phase 1 exclusively.
ACTIVE_PHASE = 1


def active_phase() -> PhaseConfig:
    """Return the currently active phase configuration."""
    return PHASES[ACTIVE_PHASE]
