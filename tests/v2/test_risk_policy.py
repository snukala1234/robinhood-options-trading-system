"""Section 3 guardrail integrity — values, completeness, and tunable non-overlap."""

from __future__ import annotations

from src.config import risk_policy, strategy_registry
from src.config.tunables import DEFAULT_TUNABLES, TUNABLE_NAMES, TunableParams

# The Section 3 policy, verbatim. If someone edits risk_policy.py, this test fails
# and the change must be justified against the spec — never the other way around.
EXPECTED_POLICY: dict[str, object] = {
    "PAPER_TRADING": True,
    "ORDER_MODE": "research_only",
    "ALLOW_LIVE_ORDERS": False,
    "TARGET_DTE_MIN": 7,
    "TARGET_DTE_MAX": 28,
    "ABSOLUTE_DTE_MIN": 5,
    "ABSOLUTE_DTE_MAX": 35,
    "MAX_RISK_PER_TRADE_PCT": 0.01,
    "MAX_TOTAL_OPEN_RISK_PCT": 0.05,
    "MAX_DAILY_REALIZED_LOSS_PCT": 0.02,
    "MAX_DAILY_EQUITY_DRAWDOWN_PCT": 0.025,
    "MAX_WEEKLY_DRAWDOWN_PCT": 0.05,
    "MAX_PEAK_TO_TROUGH_DRAWDOWN_PCT": 0.10,
    "MAX_CONCURRENT_POSITIONS": 3,
    "MAX_CORRELATED_CLUSTER_RISK_PCT": 0.02,
    "MAX_SINGLE_UNDERLYING_RISK_PCT": 0.015,
    "MAX_SINGLE_SECTOR_RISK_PCT": 0.03,
    "MIN_OPTION_OPEN_INTEREST": 100,
    "MIN_OPTION_DAILY_VOLUME": 20,
    "MAX_BID_ASK_SPREAD_PCT": 0.12,
    "MAX_QUOTE_AGE_SECONDS": 5,
    "MAX_UNDERLYING_DATA_AGE_SECONDS": 10,
    "MIN_CONTRACT_PRICE": 0.10,
    "MAX_CONTRACT_PRICE_PCT_OF_EQUITY": 0.03,
    "MIN_OPPORTUNITY_SCORE": 75.0,
    "MIN_CALIBRATED_PROBABILITY": 0.60,
    "MIN_EXPECTED_REWARD_TO_RISK": 1.5,
    "MIN_EXPECTED_VALUE_AFTER_COSTS": 0.0,
    "MAX_NET_ABS_DELTA_PCT": 0.35,
    "MAX_PORTFOLIO_GAMMA": None,
    "MAX_DAILY_THETA_BURN_PCT": 0.004,
    "MAX_ABS_VEGA_PCT": None,
    "ENFORCE_SETTLED_CASH_ONLY": True,
    "REQUIRE_DEFINED_MAX_LOSS": True,
    "ALLOW_UNDEFINED_RISK_STRATEGIES": False,
    "ALLOW_NAKED_SHORT_OPTIONS": False,
    "ALLOW_MARKET_ORDERS": False,
    "ALLOW_ZERO_DTE": False,
    "ALLOW_EARNINGS_HOLD": False,
    "ALLOW_NEW_ENTRY_DURING_FAILOVER": False,
    "REQUIRE_MANUAL_RESUME_AFTER_HALT": True,
    "REQUIRE_HUMAN_APPROVAL_FOR_CONFIG_PROMOTION": True,
    "REQUIRE_HUMAN_APPROVAL_FOR_EVERY_LIVE_ORDER": True,
}


def test_policy_values_match_spec_exactly() -> None:
    for name, expected in EXPECTED_POLICY.items():
        assert getattr(risk_policy, name) == expected, name
        # Booleans must be booleans, not truthy ints.
        if isinstance(expected, bool):
            assert isinstance(getattr(risk_policy, name), bool), name


def test_operating_mode_flags_never_drift() -> None:
    """The three build-long invariants, asserted on their own for visibility."""
    assert risk_policy.PAPER_TRADING is True
    assert risk_policy.ALLOW_LIVE_ORDERS is False
    assert risk_policy.ORDER_MODE == "research_only"


def test_guardrail_names_cover_every_policy_constant() -> None:
    assert frozenset(EXPECTED_POLICY) == risk_policy.GUARDRAIL_NAMES
    for name in risk_policy.GUARDRAIL_NAMES:
        assert hasattr(risk_policy, name), f"guardrail {name} not defined"


def test_precedence_and_kill_switches() -> None:
    assert risk_policy.GUARDRAIL_PRECEDENCE == (
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
    assert len(risk_policy.KILL_SWITCHES) == 11
    assert "manual_emergency_stop" in risk_policy.KILL_SWITCHES


def test_tunables_never_overlap_guardrails() -> None:
    assert TUNABLE_NAMES.isdisjoint(risk_policy.GUARDRAIL_NAMES)


def test_tunables_clamp_to_preapproved_ranges() -> None:
    wild = TunableParams(
        weight_directional_edge=1000.0,
        profit_target_pct_of_max_gain=0.0,
        dte_forced_exit=100,
    )
    clamped = wild.clamp_to_ranges()
    assert clamped.weight_directional_edge == 25.0
    assert clamped.profit_target_pct_of_max_gain == 0.25
    assert clamped.dte_forced_exit == 4
    # Defaults are already in range: clamping is the identity.
    assert DEFAULT_TUNABLES.clamp_to_ranges() == DEFAULT_TUNABLES


def test_tunables_roundtrip_dict() -> None:
    assert TunableParams.from_dict(DEFAULT_TUNABLES.to_dict()) == DEFAULT_TUNABLES


def test_strategy_registry_is_defined_risk_only() -> None:
    for name, spec in strategy_registry.STRATEGY_REGISTRY.items():
        assert spec.defined_risk, name
        assert spec.legs >= 1, name
        assert spec.requires, name


def test_capability_gating_filters_unsupported_strategies() -> None:
    assert strategy_registry.supported_strategies(frozenset()) == frozenset()
    single_leg = frozenset({"buy_to_open_call", "buy_to_open_put"})
    assert strategy_registry.supported_strategies(single_leg) == {"long_call", "long_put"}
    with_spreads = single_leg | {"multi_leg_options", "debit_spread"}
    assert strategy_registry.supported_strategies(with_spreads) == {
        "long_call",
        "long_put",
        "bull_call_debit_spread",
        "bear_put_debit_spread",
    }
