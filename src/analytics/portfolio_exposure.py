"""Portfolio Greeks, concentration, limit headroom, and stress (spec 5.7) — Decimal.

Dollar-Greek conventions, defined once:

- ``delta_dollars`` = per-share net delta x spot x multiplier x quantity.
  P&L for a fractional move m is approximately ``delta_dollars * m``.
- ``gamma_dollars`` = 0.5 x gamma x spot^2 x multiplier x quantity.
  The quadratic P&L term for move m is ``gamma_dollars * m^2``.
- ``theta_dollars_daily`` = per-share daily theta x multiplier x quantity
  (negative = paying decay).
- ``vega_dollars_per_pct`` = per-share vega (per 1 IV point) x multiplier x quantity.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from src.config.risk_policy import (
    MAX_DAILY_THETA_BURN_PCT,
    MAX_NET_ABS_DELTA_PCT,
    MAX_SINGLE_SECTOR_RISK_PCT,
    MAX_SINGLE_UNDERLYING_RISK_PCT,
    MAX_TOTAL_OPEN_RISK_PCT,
)
from src.domain.values import (
    DomainValidationError,
    require_money,
    require_positive_int,
    require_positive_money,
)

_TWO_DP = Decimal("0.01")


@dataclass(frozen=True)
class PositionExposure:
    """One open position's per-unit Greeks plus sizing/identity for aggregation."""

    position_id: str
    underlying: str
    sector: str
    strategy: str
    expiration: date
    spot: Decimal
    net_delta_per_unit: Decimal  # per structure unit, per share
    net_gamma_per_unit: Decimal
    net_theta_daily_per_unit: Decimal
    net_vega_per_unit: Decimal
    quantity: int
    multiplier: int
    max_loss: Decimal  # total dollars for the position

    def __post_init__(self) -> None:
        require_positive_money("spot", self.spot)
        for name in (
            "net_delta_per_unit",
            "net_gamma_per_unit",
            "net_theta_daily_per_unit",
            "net_vega_per_unit",
        ):
            require_money(name, getattr(self, name))
        require_positive_int("quantity", self.quantity)
        require_positive_int("multiplier", self.multiplier)
        require_positive_money("max_loss", self.max_loss)

    @property
    def scale(self) -> Decimal:
        return Decimal(self.multiplier) * Decimal(self.quantity)

    @property
    def delta_dollars(self) -> Decimal:
        return self.net_delta_per_unit * self.spot * self.scale

    @property
    def gamma_dollars(self) -> Decimal:
        return Decimal("0.5") * self.net_gamma_per_unit * self.spot * self.spot * self.scale

    @property
    def theta_dollars_daily(self) -> Decimal:
        return self.net_theta_daily_per_unit * self.scale

    @property
    def vega_dollars_per_pct(self) -> Decimal:
        return self.net_vega_per_unit * self.scale


@dataclass(frozen=True)
class LimitCheck:
    name: str
    value: Decimal
    limit: Decimal
    exceeded: bool


@dataclass(frozen=True)
class PortfolioExposure:
    net_delta_dollars: Decimal
    gross_delta_dollars: Decimal
    gamma_dollars: Decimal
    theta_dollars_daily: Decimal
    vega_dollars_per_pct: Decimal
    open_risk: Decimal
    risk_by_underlying: dict[str, Decimal]
    risk_by_sector: dict[str, Decimal]
    risk_by_strategy: dict[str, Decimal]
    risk_by_expiration: dict[date, Decimal]
    limit_checks: tuple[LimitCheck, ...]

    def breached_limits(self) -> tuple[LimitCheck, ...]:
        return tuple(c for c in self.limit_checks if c.exceeded)


def _bucket(positions: Sequence[PositionExposure], key: str) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for p in positions:
        k = str(getattr(p, key))
        out[k] = out.get(k, Decimal("0")) + p.max_loss
    return out


def aggregate(
    positions: Sequence[PositionExposure], *, account_equity: Decimal
) -> PortfolioExposure:
    """Aggregate positions into portfolio exposures and check policy headroom."""
    require_positive_money("account_equity", account_equity)

    net_delta = sum((p.delta_dollars for p in positions), Decimal("0"))
    gross_delta = sum((abs(p.delta_dollars) for p in positions), Decimal("0"))
    gamma = sum((p.gamma_dollars for p in positions), Decimal("0"))
    theta = sum((p.theta_dollars_daily for p in positions), Decimal("0"))
    vega = sum((p.vega_dollars_per_pct for p in positions), Decimal("0"))
    open_risk = sum((p.max_loss for p in positions), Decimal("0"))

    by_underlying = _bucket(positions, "underlying")
    by_sector = _bucket(positions, "sector")
    by_strategy = _bucket(positions, "strategy")
    by_expiration: dict[date, Decimal] = {}
    for p in positions:
        by_expiration[p.expiration] = by_expiration.get(p.expiration, Decimal("0")) + p.max_loss

    checks = [
        LimitCheck(
            name="net_abs_delta_pct",
            value=abs(net_delta) / account_equity,
            limit=Decimal(str(MAX_NET_ABS_DELTA_PCT)),
            exceeded=abs(net_delta) / account_equity > Decimal(str(MAX_NET_ABS_DELTA_PCT)),
        ),
        LimitCheck(
            name="daily_theta_burn_pct",
            value=max(-theta, Decimal("0")) / account_equity,
            limit=Decimal(str(MAX_DAILY_THETA_BURN_PCT)),
            exceeded=max(-theta, Decimal("0")) / account_equity
            > Decimal(str(MAX_DAILY_THETA_BURN_PCT)),
        ),
        LimitCheck(
            name="total_open_risk_pct",
            value=open_risk / account_equity,
            limit=Decimal(str(MAX_TOTAL_OPEN_RISK_PCT)),
            exceeded=open_risk / account_equity > Decimal(str(MAX_TOTAL_OPEN_RISK_PCT)),
        ),
    ]
    underlying_limit = Decimal(str(MAX_SINGLE_UNDERLYING_RISK_PCT))
    for name, risk in sorted(by_underlying.items()):
        checks.append(
            LimitCheck(
                name=f"underlying_risk_pct:{name}",
                value=risk / account_equity,
                limit=underlying_limit,
                exceeded=risk / account_equity > underlying_limit,
            )
        )
    sector_limit = Decimal(str(MAX_SINGLE_SECTOR_RISK_PCT))
    for name, risk in sorted(by_sector.items()):
        checks.append(
            LimitCheck(
                name=f"sector_risk_pct:{name}",
                value=risk / account_equity,
                limit=sector_limit,
                exceeded=risk / account_equity > sector_limit,
            )
        )

    return PortfolioExposure(
        net_delta_dollars=net_delta,
        gross_delta_dollars=gross_delta,
        gamma_dollars=gamma,
        theta_dollars_daily=theta,
        vega_dollars_per_pct=vega,
        open_risk=open_risk,
        risk_by_underlying=by_underlying,
        risk_by_sector=by_sector,
        risk_by_strategy=by_strategy,
        risk_by_expiration=by_expiration,
        limit_checks=tuple(checks),
    )


#: Default stress scenarios: fractional underlying moves applied to every position.
DEFAULT_STRESS_MOVES: tuple[Decimal, ...] = (
    Decimal("-0.10"),
    Decimal("-0.05"),
    Decimal("-0.02"),
    Decimal("0.02"),
    Decimal("0.05"),
    Decimal("0.10"),
)


@dataclass(frozen=True)
class StressScenario:
    move: Decimal  # fractional underlying move, e.g. -0.05
    estimated_pnl: Decimal  # delta-gamma approximation, dollars
    bounded_by_max_loss: Decimal  # estimate floored at -total open risk


def stress_scenarios(
    positions: Sequence[PositionExposure],
    moves: Sequence[Decimal] = DEFAULT_STRESS_MOVES,
) -> tuple[StressScenario, ...]:
    """Delta-gamma stress estimates. Losses are floored at total defined max loss —
    the one thing a defined-risk book guarantees."""
    if not moves:
        raise DomainValidationError("at least one stress move required")
    open_risk = sum((p.max_loss for p in positions), Decimal("0"))
    scenarios: list[StressScenario] = []
    for move in moves:
        require_money("move", move)
        if not (Decimal("-1") < move < Decimal("1")):
            raise DomainValidationError(f"stress move {move} out of (-1, 1)")
        pnl = sum(
            (p.delta_dollars * move + p.gamma_dollars * move * move for p in positions),
            Decimal("0"),
        )
        scenarios.append(
            StressScenario(
                move=move,
                estimated_pnl=pnl.quantize(_TWO_DP),
                bounded_by_max_loss=max(pnl, -open_risk).quantize(_TWO_DP),
            )
        )
    return tuple(scenarios)
