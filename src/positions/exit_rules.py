"""The five Section 10 exit dimensions, evaluated deterministically.

Each dimension reads typed keys from the position's :class:`ExitPlan` (built
by :func:`build_exit_plan`, stored JSON-safe with money as decimal strings)
plus the validated :class:`PositionMarketState`, and emits zero or more
:class:`ExitSignal` objects. Signals aggregate by action precedence
``EXIT > REDUCE > REVIEW > ALERT > HOLD``; urgency is the maximum among the
winning action's signals. A malformed plan value (missing key, float where a
decimal string belongs) raises — it never evaluates to "no exit".

:func:`exit_limit_price` prices the closing order slippage-aware: limit
orders only, starting from the midpoint and conceding a bounded fraction of
the spread — more under high urgency, but never a market order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from src.config.risk_policy import MAX_BID_ASK_SPREAD_PCT
from src.config.tunables import DEFAULT_TUNABLES, TunableParams
from src.domain.positions import ExitPlan
from src.domain.values import (
    DomainValidationError,
    require_non_negative_money,
    require_positive_int,
    require_positive_money,
)
from src.execution.interface import NetIntent
from src.positions.monitoring import PositionMarketState

Urgency = Literal["low", "medium", "high"]
Action = Literal["hold", "alert", "review", "reduce", "exit"]

_ACTION_RANK: dict[str, int] = {"hold": 0, "alert": 1, "review": 2, "reduce": 3, "exit": 4}
_URGENCY_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}

_CENT = Decimal("0.01")


@dataclass(frozen=True)
class ExitSignal:
    dimension: Literal["premium", "underlying", "time", "volatility", "event"]
    rule: str
    action: Action
    urgency: Urgency
    detail: str


@dataclass(frozen=True)
class ExitEvaluation:
    signals: tuple[ExitSignal, ...]
    action: Action
    urgency: Urgency
    unrealized_pnl: Decimal

    @property
    def should_exit(self) -> bool:
        return self.action == "exit"


def _plan_dec(plan: dict[str, Any], key: str) -> Decimal:
    value = plan.get(key)
    if value is None:
        raise DomainValidationError(f"exit plan missing required key {key!r}")
    return _to_dec(key, value)


def _plan_dec_opt(plan: dict[str, Any], key: str) -> Decimal | None:
    value = plan.get(key)
    return None if value is None else _to_dec(key, value)


def _to_dec(key: str, value: Any) -> Decimal:
    if isinstance(value, float | bool):
        raise DomainValidationError(
            f"exit plan key {key!r} must be a decimal string, got {value!r}"
        )
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise DomainValidationError(f"exit plan key {key!r} is not numeric: {value!r}") from exc


def build_exit_plan(
    *,
    direction: Literal["bullish", "bearish"],
    invalidation_level: Decimal,
    max_loss_exit_usd: Decimal,
    max_holding_days: int,
    long_vega: bool,
    event_exit_days_before: int = 3,
    profit_target_net_price: Decimal | None = None,
    scale_out_net_price: Decimal | None = None,
    support_level: Decimal | None = None,
    resistance_level: Decimal | None = None,
    requires_breakout_above: Decimal | None = None,
    entry_theta_daily_per_unit: Decimal | None = None,
    entry_iv: Decimal | None = None,
    iv_crush_exit_drop_pct: Decimal = Decimal("0.25"),
    iv_expansion_take_profit_pct: Decimal = Decimal("0.30"),
    vega_floor_per_unit: Decimal | None = None,
) -> ExitPlan:
    """Construct a complete five-dimension plan with JSON-safe typed keys."""
    if direction not in ("bullish", "bearish"):
        raise DomainValidationError(f"direction must be bullish/bearish, got {direction!r}")
    require_positive_money("invalidation_level", invalidation_level)
    require_positive_money("max_loss_exit_usd", max_loss_exit_usd)
    require_positive_int("max_holding_days", max_holding_days)
    require_positive_int("event_exit_days_before", event_exit_days_before)
    require_positive_money("iv_crush_exit_drop_pct", iv_crush_exit_drop_pct)
    require_positive_money("iv_expansion_take_profit_pct", iv_expansion_take_profit_pct)

    def _opt(name: str, value: Decimal | None) -> str | None:
        if value is None:
            return None
        require_positive_money(name, value)
        return str(value)

    premium: dict[str, Any] = {"max_loss_exit_usd": str(max_loss_exit_usd)}
    if profit_target_net_price is not None:
        premium["profit_target_net_price"] = _opt(
            "profit_target_net_price", profit_target_net_price
        )
    if scale_out_net_price is not None:
        premium["scale_out_net_price"] = _opt("scale_out_net_price", scale_out_net_price)

    underlying: dict[str, Any] = {
        "direction": direction,
        "invalidation_level": str(invalidation_level),
    }
    if support_level is not None:
        underlying["support_level"] = _opt("support_level", support_level)
    if resistance_level is not None:
        underlying["resistance_level"] = _opt("resistance_level", resistance_level)
    if requires_breakout_above is not None:
        underlying["requires_breakout_above"] = _opt(
            "requires_breakout_above", requires_breakout_above
        )

    time_plan: dict[str, Any] = {"max_holding_days": max_holding_days}
    if entry_theta_daily_per_unit is not None:
        time_plan["entry_theta_daily_per_unit"] = str(entry_theta_daily_per_unit)

    volatility: dict[str, Any] = {
        "long_vega": long_vega,
        "iv_crush_exit_drop_pct": str(iv_crush_exit_drop_pct),
        "iv_expansion_take_profit_pct": str(iv_expansion_take_profit_pct),
    }
    if entry_iv is not None:
        volatility["entry_iv"] = _opt("entry_iv", entry_iv)
    if vega_floor_per_unit is not None:
        volatility["vega_floor_per_unit"] = _opt("vega_floor_per_unit", vega_floor_per_unit)

    event: dict[str, Any] = {"event_exit_days_before": event_exit_days_before}

    return ExitPlan(
        premium=premium,
        underlying=underlying,
        time=time_plan,
        volatility=volatility,
        event=event,
    )


# --- dimension evaluators -----------------------------------------------------


def _premium_signals(state: PositionMarketState) -> list[ExitSignal]:
    plan = state.position.exit_plan.premium
    signals: list[ExitSignal] = []
    max_loss_exit = _plan_dec(plan, "max_loss_exit_usd")
    if state.unrealized_loss_total >= max_loss_exit:
        signals.append(
            ExitSignal(
                "premium",
                "hard_max_loss_threshold",
                "exit",
                "high",
                f"unrealized loss {state.unrealized_loss_total} >= plan threshold {max_loss_exit}",
            )
        )
    target = _plan_dec_opt(plan, "profit_target_net_price")
    if target is not None:
        hit = (
            state.current_net_price >= target
            if state.opened_intent is NetIntent.DEBIT
            else state.current_net_price <= target
        )
        if hit:
            signals.append(
                ExitSignal(
                    "premium",
                    "profit_target",
                    "exit",
                    "low",
                    f"mark {state.current_net_price} reached target {target}",
                )
            )
    scale = _plan_dec_opt(plan, "scale_out_net_price")
    if scale is not None:
        hit = (
            state.current_net_price >= scale
            if state.opened_intent is NetIntent.DEBIT
            else state.current_net_price <= scale
        )
        if hit:
            signals.append(
                ExitSignal(
                    "premium",
                    "scale_out",
                    "reduce",
                    "low",
                    f"mark {state.current_net_price} reached scale-out level {scale}",
                )
            )
    return signals


def _underlying_signals(state: PositionMarketState) -> list[ExitSignal]:
    plan = state.position.exit_plan.underlying
    direction = plan.get("direction")
    if direction not in ("bullish", "bearish"):
        raise DomainValidationError(f"exit plan underlying.direction invalid: {direction!r}")
    bullish = direction == "bullish"
    signals: list[ExitSignal] = []

    invalidation = _plan_dec(plan, "invalidation_level")
    if (bullish and state.spot <= invalidation) or (not bullish and state.spot >= invalidation):
        signals.append(
            ExitSignal(
                "underlying",
                "thesis_invalidated",
                "exit",
                "high",
                f"spot {state.spot} crossed invalidation level {invalidation}",
            )
        )
    breakout = _plan_dec_opt(plan, "requires_breakout_above")
    if breakout is not None and state.spot < breakout:
        signals.append(
            ExitSignal(
                "underlying",
                "breakout_failure",
                "exit",
                "medium",
                f"spot {state.spot} back below required breakout level {breakout}",
            )
        )
    if (bullish and state.trend_state == "down") or (not bullish and state.trend_state == "up"):
        signals.append(
            ExitSignal(
                "underlying",
                "trend_reversal",
                "review",
                "medium",
                f"trend is {state.trend_state} against a {direction} thesis",
            )
        )
    support = _plan_dec_opt(plan, "support_level")
    if bullish and support is not None and state.spot < support:
        signals.append(
            ExitSignal(
                "underlying",
                "support_violation",
                "reduce",
                "medium",
                f"spot {state.spot} below support {support}",
            )
        )
    resistance = _plan_dec_opt(plan, "resistance_level")
    if not bullish and resistance is not None and state.spot > resistance:
        signals.append(
            ExitSignal(
                "underlying",
                "resistance_violation",
                "reduce",
                "medium",
                f"spot {state.spot} above resistance {resistance}",
            )
        )
    return signals


def _time_signals(state: PositionMarketState, tunables: TunableParams) -> list[ExitSignal]:
    plan = state.position.exit_plan.time
    max_days = plan.get("max_holding_days")
    if not isinstance(max_days, int) or isinstance(max_days, bool) or max_days < 1:
        raise DomainValidationError(f"exit plan time.max_holding_days invalid: {max_days!r}")
    signals: list[ExitSignal] = []
    if state.holding_days >= max_days:
        signals.append(
            ExitSignal(
                "time",
                "max_holding_duration",
                "exit",
                "medium",
                f"held {state.holding_days}d >= max {max_days}d",
            )
        )
    if state.dte <= tunables.dte_forced_exit:
        signals.append(
            ExitSignal(
                "time",
                "exit_before_expiration",
                "exit",
                "high",
                f"dte {state.dte} <= forced-exit threshold {tunables.dte_forced_exit}",
            )
        )
    elif state.dte <= tunables.dte_review_checkpoint:
        signals.append(
            ExitSignal(
                "time",
                "mandatory_dte_review",
                "review",
                "medium",
                f"dte {state.dte} <= review checkpoint {tunables.dte_review_checkpoint}",
            )
        )
    entry_theta = _plan_dec_opt(plan, "entry_theta_daily_per_unit")
    if (
        entry_theta is not None
        and entry_theta != 0
        and state.current_theta_daily_per_unit is not None
        and abs(state.current_theta_daily_per_unit) >= 2 * abs(entry_theta)
    ):
        signals.append(
            ExitSignal(
                "time",
                "accelerating_theta",
                "alert",
                "low",
                f"daily theta {state.current_theta_daily_per_unit} is >= 2x entry {entry_theta}",
            )
        )
    return signals


def _volatility_signals(state: PositionMarketState) -> list[ExitSignal]:
    plan = state.position.exit_plan.volatility
    long_vega = plan.get("long_vega")
    if not isinstance(long_vega, bool):
        raise DomainValidationError(f"exit plan volatility.long_vega invalid: {long_vega!r}")
    signals: list[ExitSignal] = []
    entry_iv = _plan_dec_opt(plan, "entry_iv")
    if entry_iv is not None:
        if state.current_iv is None:
            signals.append(
                ExitSignal(
                    "volatility",
                    "iv_unavailable",
                    "alert",
                    "low",
                    "plan tracks IV but no current IV is available",
                )
            )
        else:
            crush_drop = _plan_dec(plan, "iv_crush_exit_drop_pct")
            expansion = _plan_dec(plan, "iv_expansion_take_profit_pct")
            if long_vega and state.current_iv <= entry_iv * (1 - crush_drop):
                signals.append(
                    ExitSignal(
                        "volatility",
                        "iv_crush",
                        "exit",
                        "medium",
                        f"IV {state.current_iv} fell >= {crush_drop} from entry {entry_iv}",
                    )
                )
            if long_vega and state.current_iv >= entry_iv * (1 + expansion):
                signals.append(
                    ExitSignal(
                        "volatility",
                        "favorable_vol_expansion",
                        "review",
                        "low",
                        f"IV {state.current_iv} expanded >= {expansion} above entry {entry_iv};"
                        " consider taking profit",
                    )
                )
    if state.vol_regime_changed:
        signals.append(
            ExitSignal(
                "volatility",
                "vol_regime_change",
                "review",
                "medium",
                "skew/term-structure regime changed since entry",
            )
        )
    vega_floor = _plan_dec_opt(plan, "vega_floor_per_unit")
    if (
        vega_floor is not None
        and state.current_vega_per_unit is not None
        and abs(state.current_vega_per_unit) < vega_floor
    ):
        signals.append(
            ExitSignal(
                "volatility",
                "vega_exhausted",
                "review",
                "low",
                f"remaining |vega| {abs(state.current_vega_per_unit)} < floor {vega_floor};"
                " the vol thesis can no longer pay",
            )
        )
    return signals


def _event_signals(state: PositionMarketState) -> list[ExitSignal]:
    plan = state.position.exit_plan.event
    days_before = plan.get("event_exit_days_before")
    if not isinstance(days_before, int) or isinstance(days_before, bool) or days_before < 1:
        raise DomainValidationError(
            f"exit plan event.event_exit_days_before invalid: {days_before!r}"
        )
    signals: list[ExitSignal] = []
    if state.catalyst_completed:
        signals.append(
            ExitSignal(
                "event",
                "catalyst_completed",
                "review",
                "medium",
                "the catalyst this position was entered for has occurred",
            )
        )
    if state.new_material_event:
        signals.append(
            ExitSignal(
                "event",
                "new_material_event",
                "exit",
                "high",
                "a new material event invalidates the thesis",
            )
        )
    event_date = state.next_scheduled_event_date
    if event_date is not None:
        window_end = state.as_of.date() + timedelta(days=days_before)
        if event_date <= window_end and event_date <= state.position.expiration:
            signals.append(
                ExitSignal(
                    "event",
                    "event_prohibited_window",
                    "exit",
                    "high",
                    f"scheduled event {event_date.isoformat()} inside the prohibited window"
                    " (ALLOW_EARNINGS_HOLD is False)",
                )
            )
    if state.trading_halted:
        signals.append(
            ExitSignal(
                "event",
                "trading_halt",
                "alert",
                "high",
                "underlying is halted; no order can responsibly be priced",
            )
        )
    else:
        spread = state.ask - state.bid
        mid = (state.bid + state.ask) / 2
        abnormal = state.bid == 0 or (
            mid > 0 and spread / mid > 2 * Decimal(str(MAX_BID_ASK_SPREAD_PCT))
        )
        if abnormal:
            signals.append(
                ExitSignal(
                    "event",
                    "abnormal_liquidity",
                    "alert",
                    "high",
                    f"bid {state.bid} / ask {state.ask} is abnormally wide or one-sided",
                )
            )
    return signals


def evaluate_exits(
    state: PositionMarketState, tunables: TunableParams = DEFAULT_TUNABLES
) -> ExitEvaluation:
    """Run all five dimensions and aggregate by action precedence."""
    signals: list[ExitSignal] = []
    signals.extend(_premium_signals(state))
    signals.extend(_underlying_signals(state))
    signals.extend(_time_signals(state, tunables))
    signals.extend(_volatility_signals(state))
    signals.extend(_event_signals(state))

    if not signals:
        return ExitEvaluation((), "hold", "low", state.unrealized_pnl_total)
    winning: Action = max(signals, key=lambda s: _ACTION_RANK[s.action]).action
    urgency: Urgency = max(
        (s for s in signals if s.action == winning),
        key=lambda s: _URGENCY_RANK[s.urgency],
    ).urgency
    return ExitEvaluation(tuple(signals), winning, urgency, state.unrealized_pnl_total)


def exit_limit_price(
    bid: Decimal, ask: Decimal, *, opened_intent: NetIntent, urgency: Urgency = "medium"
) -> Decimal:
    """Slippage-aware limit for a closing order: midpoint minus a bounded spread
    concession (never a market order). High urgency concedes half the spread;
    otherwise a quarter. Selling to close a debit structure concedes downward;
    buying to close a credit structure concedes upward."""
    require_non_negative_money("bid", bid)
    require_positive_money("ask", ask)
    if bid > ask:
        raise DomainValidationError(f"crossed market: bid {bid} > ask {ask}")
    if urgency not in ("low", "medium", "high"):
        raise DomainValidationError(f"urgency invalid: {urgency!r}")
    spread = ask - bid
    mid = (bid + ask) / 2
    concession = spread * (Decimal("0.5") if urgency == "high" else Decimal("0.25"))
    price = mid - concession if opened_intent is NetIntent.DEBIT else mid + concession
    return max(price.quantize(_CENT), _CENT)
