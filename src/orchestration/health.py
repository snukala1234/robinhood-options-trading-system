"""Individual health probes, reusable at startup and during the session.

Every probe returns a :class:`CheckResult` and never raises — a probe that
blows up reports itself as failed with the error in ``detail``. The truthful
answer to "is this healthy?" when the probe itself breaks is "no".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from src.data.option_chains import ContractQuote
from src.execution.interface import AccountSnapshot, BrokerInterface
from src.persistence.db import Connection

#: All fourteen Section 14 tables that must exist after migration.
EXPECTED_TABLES = frozenset(
    {
        "broker_capability_snapshots",
        "market_data_snapshots",
        "option_contract_snapshots",
        "strategy_config_versions",
        "opportunity_candidates",
        "trade_proposals",
        "orders",
        "order_events",
        "positions",
        "position_snapshots",
        "portfolio_snapshots",
        "agent_decisions",
        "calibration_results",
        "system_events",
    }
)

#: Maximum tolerated skew between the app clock and the database clock.
MAX_CLOCK_SKEW_SECONDS = 5.0


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


def check_database(conn: Connection) -> CheckResult:
    """Connectivity, migrations applied, and all Section 14 tables present."""
    name = "database_health"
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        if row is None:
            return CheckResult(name, False, "alembic_version is empty; migrations not applied")
        cur = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
        present = {str(r["table_name"]) for r in cur.fetchall()}
        missing = EXPECTED_TABLES - present
        if missing:
            return CheckResult(name, False, f"missing tables: {sorted(missing)}")
        return CheckResult(name, True, f"migrated at {row['version_num']}, 14 tables present")
    except Exception as exc:  # noqa: BLE001 - a broken probe means unhealthy
        return CheckResult(name, False, f"database probe failed: {exc}")


def check_clock_sync(conn: Connection, now: datetime) -> CheckResult:
    """App clock vs. the database clock — the one independent clock we have."""
    name = "clock_synchronization"
    try:
        row = conn.execute("SELECT now() AS db_now").fetchone()
        assert row is not None
        skew = abs((row["db_now"] - now).total_seconds())
        if skew > MAX_CLOCK_SKEW_SECONDS:
            return CheckResult(name, False, f"clock skew {skew:.1f}s > {MAX_CLOCK_SKEW_SECONDS}s")
        return CheckResult(name, True, f"skew {skew:.1f}s")
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, False, f"clock probe failed: {exc}")


def check_broker_auth(broker: BrokerInterface) -> CheckResult:
    """An authenticated account snapshot is the proof of broker access."""
    name = "broker_authentication"
    try:
        snapshot: AccountSnapshot = broker.account_snapshot()
        return CheckResult(name, True, f"account {snapshot.account_id_hash[:12]}... reachable")
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, False, f"broker unreachable: {exc}")


def check_market_data(
    probe: Callable[[], ContractQuote], clock: Callable[[], datetime]
) -> CheckResult:
    """The feed must produce a quote that passes the hard freshness gate.

    Freshness is judged at the moment the probe returns (a fetch takes time;
    judging against an earlier snapshot would misread a fresh quote as clock
    skew)."""
    name = "market_data_feed"
    try:
        quote = probe()
        quote.require_fresh(clock())
        return CheckResult(
            name, True, f"fresh quote for {quote.contract.occ_symbol()} from {quote.source}"
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, False, f"market data invalid: {exc}")
