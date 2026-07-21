"""Section 0 — non-negotiable, hard-coded guardrails.

These live in plain code, never in a prompt, and NEVER behind an LLM's judgment.
No agent — including the Agent 8 learning loop — may modify any value in this module.
Agent 8 may only *recommend* a change with evidence (Section 0 / Section 3.6); applying
one is a deliberate, logged human action. The tunable parameters Agent 8 *is* allowed to
adjust (within pre-approved ranges, via shadow-testing) live separately in
``config.strategy`` and deliberately do not overlap with anything here.

Concrete value note: the spec gives ``HARD_STOP_LOSS_PCT`` as a 0.15–0.20 band. A single
concrete value is required in code; 0.18 (mid-band) is used and must stay within the band.
"""

from __future__ import annotations

# --- per-position / portfolio hard limits (Section 0) ----------------------
HARD_STOP_LOSS_PCT = 0.18            # per position; band 0.15–0.20; enforced by code
HARD_STOP_LOSS_BAND = (0.15, 0.20)  # documented allowable band for the concrete value
MAX_POSITION_PCT_OF_EQUITY = 0.40   # Phase 1
MAX_CONCURRENT_POSITIONS = 3        # Phase 1
MAX_DAILY_LOSS_PCT = 0.10           # halts all new entries for the day if breached
MAX_DRAWDOWN_HALT_PCT = 0.25        # from high-water mark; halts trading, manual resume
MIN_SIGNAL_CONFIDENCE_TO_TRADE = 0.65   # calibrated confidence, not raw model output

# --- order approval / trading mode -----------------------------------------
ORDER_APPROVAL_MODE = "manual"      # every order requires explicit approval in Phase 1
PAPER_TRADING = True                # no live orders anywhere in the build

# --- cash-account settlement (Section 0 / 1.1) -----------------------------
# PDT day-trade count does NOT apply to a cash account; the binding constraint is
# T+1 settlement. Only settled cash may fund a purchase.
MAX_DAY_TRADES_PER_5_SESSIONS = None    # not applicable to a cash account
ENFORCE_SETTLED_CASH_ONLY = True        # never commit unsettled proceeds to a new purchase

# --- learning-loop safety gate (Section 3.2) -------------------------------
# Do not adjust anything below this sample size. Gates all of Agent 8's adaptation.
MIN_SAMPLE_SIZE_FOR_ADAPTATION = 30

# Names of the Section 0 hard guardrails. Used by tests / Agent 8 to assert that the
# tunable-parameter set never overlaps these, i.e. adaptation can never touch a guardrail.
HARD_GUARDRAIL_NAMES = frozenset(
    {
        "HARD_STOP_LOSS_PCT",
        "MAX_POSITION_PCT_OF_EQUITY",
        "MAX_CONCURRENT_POSITIONS",
        "MAX_DAILY_LOSS_PCT",
        "MAX_DRAWDOWN_HALT_PCT",
        "MIN_SIGNAL_CONFIDENCE_TO_TRADE",
        "ORDER_APPROVAL_MODE",
        "PAPER_TRADING",
        "ENFORCE_SETTLED_CASH_ONLY",
        "MIN_SAMPLE_SIZE_FOR_ADAPTATION",
    }
)


def hard_stop_within_band() -> bool:
    """True iff the concrete ``HARD_STOP_LOSS_PCT`` is within its documented band."""
    lo, hi = HARD_STOP_LOSS_BAND
    return lo <= HARD_STOP_LOSS_PCT <= hi
