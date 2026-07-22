"""Section 3 — hard-coded guardrails. Pure code, never prompts, never LLM-modifiable.

Every value here is a Section 3 spec default. Agents may *recommend* changes with
evidence; applying one is a deliberate, logged human action. The tunable parameters
agents may adjust live in :mod:`src.config.tunables` and (test-enforced) never overlap
anything named in :data:`GUARDRAIL_NAMES`.

The three operating-mode flags stay at their shipped values for the entire build:
``PAPER_TRADING=True``, ``ALLOW_LIVE_ORDERS=False``, ``ORDER_MODE="research_only"``.
Changing them is a manual human step that only happens after the Section 24 validation
checklist — no code path in this repository does it.
"""

from __future__ import annotations

# --- operating mode (never changed by the build) ----------------------------
PAPER_TRADING = True
ORDER_MODE = "research_only"  # research_only | approval_required | bounded_autonomy
ALLOW_LIVE_ORDERS = False

# --- DTE rules (Section 3 / 7.2) --------------------------------------------
TARGET_DTE_MIN = 7
TARGET_DTE_MAX = 28
ABSOLUTE_DTE_MIN = 5
ABSOLUTE_DTE_MAX = 35

# --- risk budgets (fractions of account equity) -----------------------------
MAX_RISK_PER_TRADE_PCT = 0.01  # 1% of account equity at maximum loss
MAX_TOTAL_OPEN_RISK_PCT = 0.05  # sum of defined maximum losses
MAX_DAILY_REALIZED_LOSS_PCT = 0.02
MAX_DAILY_EQUITY_DRAWDOWN_PCT = 0.025
MAX_WEEKLY_DRAWDOWN_PCT = 0.05
MAX_PEAK_TO_TROUGH_DRAWDOWN_PCT = 0.10
MAX_CONCURRENT_POSITIONS = 3
MAX_CORRELATED_CLUSTER_RISK_PCT = 0.02
MAX_SINGLE_UNDERLYING_RISK_PCT = 0.015
MAX_SINGLE_SECTOR_RISK_PCT = 0.03

# --- liquidity and data-freshness floors ------------------------------------
MIN_OPTION_OPEN_INTEREST = 100
MIN_OPTION_DAILY_VOLUME = 20
MAX_BID_ASK_SPREAD_PCT = 0.12
MAX_QUOTE_AGE_SECONDS = 5
MAX_UNDERLYING_DATA_AGE_SECONDS = 10
MIN_CONTRACT_PRICE = 0.10
MAX_CONTRACT_PRICE_PCT_OF_EQUITY = 0.03

# --- opportunity quality gates ----------------------------------------------
MIN_OPPORTUNITY_SCORE = 75.0
MIN_CALIBRATED_PROBABILITY = 0.60
MIN_EXPECTED_REWARD_TO_RISK = 1.5
MIN_EXPECTED_VALUE_AFTER_COSTS = 0.0

# --- portfolio Greek limits ---------------------------------------------------
MAX_NET_ABS_DELTA_PCT = 0.35
MAX_PORTFOLIO_GAMMA = None  # derive after paper calibration
MAX_DAILY_THETA_BURN_PCT = 0.004
MAX_ABS_VEGA_PCT = None  # derive after paper calibration

# --- structural prohibitions --------------------------------------------------
ENFORCE_SETTLED_CASH_ONLY = True
REQUIRE_DEFINED_MAX_LOSS = True
ALLOW_UNDEFINED_RISK_STRATEGIES = False
ALLOW_NAKED_SHORT_OPTIONS = False
ALLOW_MARKET_ORDERS = False
ALLOW_ZERO_DTE = False
ALLOW_EARNINGS_HOLD = False
ALLOW_NEW_ENTRY_DURING_FAILOVER = False

# --- human-in-the-loop requirements ------------------------------------------
REQUIRE_MANUAL_RESUME_AFTER_HALT = True
REQUIRE_HUMAN_APPROVAL_FOR_CONFIG_PROMOTION = True
REQUIRE_HUMAN_APPROVAL_FOR_EVERY_LIVE_ORDER = True

# --- Section 3.1 guardrail precedence (mandatory decision order) -------------
# No downstream approval can override an upstream rejection.
GUARDRAIL_PRECEDENCE: tuple[str, ...] = (
    "system_health_and_data_freshness",
    "broker_account_capability",
    "settlement_and_buying_power",
    "strategy_permission",
    "per_trade_maximum_loss",
    "portfolio_exposure",
    "circuit_breakers",
    "liquidity_and_execution",
    "human_approval_policy",
    "order_submission",
)

# --- Section 3.2 kill switches ------------------------------------------------
KILL_SWITCHES: tuple[str, ...] = (
    "global_trading_halt",
    "new_entry_halt",
    "broker_degradation",
    "market_data_degradation",
    "model_provider_degradation",
    "order_state_uncertainty",
    "excessive_slippage",
    "excessive_reject_rate",
    "reconciliation_mismatch",
    "drawdown_breach",
    "manual_emergency_stop",
)

# Names of the hard guardrails. Tests assert the tunable-parameter set never overlaps
# these, i.e. adaptation can never touch a guardrail (same invariant V1 proved).
GUARDRAIL_NAMES = frozenset(
    {
        "PAPER_TRADING",
        "ORDER_MODE",
        "ALLOW_LIVE_ORDERS",
        "TARGET_DTE_MIN",
        "TARGET_DTE_MAX",
        "ABSOLUTE_DTE_MIN",
        "ABSOLUTE_DTE_MAX",
        "MAX_RISK_PER_TRADE_PCT",
        "MAX_TOTAL_OPEN_RISK_PCT",
        "MAX_DAILY_REALIZED_LOSS_PCT",
        "MAX_DAILY_EQUITY_DRAWDOWN_PCT",
        "MAX_WEEKLY_DRAWDOWN_PCT",
        "MAX_PEAK_TO_TROUGH_DRAWDOWN_PCT",
        "MAX_CONCURRENT_POSITIONS",
        "MAX_CORRELATED_CLUSTER_RISK_PCT",
        "MAX_SINGLE_UNDERLYING_RISK_PCT",
        "MAX_SINGLE_SECTOR_RISK_PCT",
        "MIN_OPTION_OPEN_INTEREST",
        "MIN_OPTION_DAILY_VOLUME",
        "MAX_BID_ASK_SPREAD_PCT",
        "MAX_QUOTE_AGE_SECONDS",
        "MAX_UNDERLYING_DATA_AGE_SECONDS",
        "MIN_CONTRACT_PRICE",
        "MAX_CONTRACT_PRICE_PCT_OF_EQUITY",
        "MIN_OPPORTUNITY_SCORE",
        "MIN_CALIBRATED_PROBABILITY",
        "MIN_EXPECTED_REWARD_TO_RISK",
        "MIN_EXPECTED_VALUE_AFTER_COSTS",
        "MAX_NET_ABS_DELTA_PCT",
        "MAX_PORTFOLIO_GAMMA",
        "MAX_DAILY_THETA_BURN_PCT",
        "MAX_ABS_VEGA_PCT",
        "ENFORCE_SETTLED_CASH_ONLY",
        "REQUIRE_DEFINED_MAX_LOSS",
        "ALLOW_UNDEFINED_RISK_STRATEGIES",
        "ALLOW_NAKED_SHORT_OPTIONS",
        "ALLOW_MARKET_ORDERS",
        "ALLOW_ZERO_DTE",
        "ALLOW_EARNINGS_HOLD",
        "ALLOW_NEW_ENTRY_DURING_FAILOVER",
        "REQUIRE_MANUAL_RESUME_AFTER_HALT",
        "REQUIRE_HUMAN_APPROVAL_FOR_CONFIG_PROMOTION",
        "REQUIRE_HUMAN_APPROVAL_FOR_EVERY_LIVE_ORDER",
    }
)
