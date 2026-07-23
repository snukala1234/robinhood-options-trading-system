"""Section 15.1 startup validation — runs before the first scan, fails closed.

All ten checks run every time (no short-circuit: the operator sees the full
picture), but ONE failure blocks the session: the report's ``passed`` is the
conjunction, and :meth:`SessionMachine.complete_startup` refuses a failed
report. The report also carries what a restart needs to recover correctly —
broker positions and open orders found during reconciliation — so a
mid-session restart resumes into POSITION_MANAGEMENT instead of pretending
it is a fresh morning.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from src.config import environments, risk_policy
from src.data.option_chains import ContractQuote
from src.execution.capabilities import CapabilitySnapshotRepository
from src.execution.interface import BrokerInterface
from src.execution.order_state_machine import OrderStateMachine
from src.execution.reconciliation import reconcile
from src.gate.kill_switches import KillSwitchPanel
from src.orchestration import calendar
from src.orchestration.config_integrity import verify_config_row
from src.orchestration.health import (
    CheckResult,
    check_broker_auth,
    check_clock_sync,
    check_database,
    check_market_data,
)
from src.persistence.db import Connection
from src.persistence.repositories import ConfigVersionRepository


@dataclass(frozen=True)
class StartupValidationReport:
    started_at: datetime
    checks: tuple[CheckResult, ...]
    mode_banner: str
    broker_positions: int
    open_orders: int

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        return tuple(f"{c.name}: {c.detail}" for c in self.checks if not c.passed)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class StartupValidator:
    """Composes the ten Section 15.1 checks over injected dependencies."""

    conn: Connection
    broker: BrokerInterface
    panel: KillSwitchPanel
    market_data_probe: Callable[[], ContractQuote]
    account_id_hash: str
    clock: Callable[[], datetime] = _utcnow

    def run(self) -> StartupValidationReport:
        now = self.clock()
        machine = OrderStateMachine(self.conn)
        checks: list[CheckResult] = []
        broker_positions = 0
        open_orders = 0

        # 1. Market calendar and session.
        try:
            day = now.astimezone(calendar.ET).date()
            phase = calendar.session_phase(now)
            trading = calendar.is_trading_day(day)
            checks.append(
                CheckResult(
                    "market_calendar_and_session",
                    True,
                    f"{day.isoformat()}: trading_day={trading}, phase={phase}",
                )
            )
        except Exception as exc:  # noqa: BLE001 - calendar gap blocks the session
            checks.append(CheckResult("market_calendar_and_session", False, str(exc)))

        # 2. Clock synchronization.  3. Database health.
        checks.append(check_clock_sync(self.conn, now))
        checks.append(check_database(self.conn))

        # 4. Broker authentication.
        checks.append(check_broker_auth(self.broker))

        # 5. Capability snapshot refresh.
        try:
            caps = self.broker.capabilities()
            snapshot_id = CapabilitySnapshotRepository(self.conn).record(
                caps, account_id_hash=self.account_id_hash
            )
            checks.append(
                CheckResult("capability_snapshot_refresh", True, f"snapshot {snapshot_id} recorded")
            )
        except Exception as exc:  # noqa: BLE001
            checks.append(CheckResult("capability_snapshot_refresh", False, str(exc)))

        # 6. Reconcile account, positions, and open orders.
        try:
            report = reconcile(machine, self.broker, now=now, conn=self.conn)
            broker_positions = len(self.broker.positions())
            open_orders = len(self.broker.open_orders())
            if report.clean:
                checks.append(
                    CheckResult(
                        "reconcile_account_positions_orders",
                        True,
                        f"{report.checked} order(s) checked, {len(report.advanced)} advanced,"
                        f" {broker_positions} broker position(s), {open_orders} open order(s)",
                    )
                )
            else:
                checks.append(
                    CheckResult(
                        "reconcile_account_positions_orders",
                        False,
                        f"unclean: {len(report.flagged)} flagged,"
                        f" {len(report.missing_at_broker)} missing at broker,"
                        f" {len(report.unknown_at_local)} unknown locally,"
                        f" {len(report.stale_submitted)} stale submitted",
                    )
                )
        except Exception as exc:  # noqa: BLE001
            checks.append(CheckResult("reconcile_account_positions_orders", False, str(exc)))

        # 7. Market-data feed validation (freshness judged at probe time).
        checks.append(check_market_data(self.market_data_probe, self.clock))

        # 8. Active config load with hash verification.
        try:
            row = ConfigVersionRepository(self.conn).active_version()
            if row is None:
                checks.append(
                    CheckResult("active_config_integrity", False, "no active config version")
                )
            else:
                ok, detail = verify_config_row(row)
                checks.append(CheckResult("active_config_integrity", ok, detail))
        except Exception as exc:  # noqa: BLE001
            checks.append(CheckResult("active_config_integrity", False, str(exc)))

        # 9. Paper/live state, confirmed visibly.
        live_possible = environments.live_orders_permitted()
        mode_banner = (
            f"MODE: {'PAPER' if risk_policy.PAPER_TRADING else 'LIVE'} | "
            f"ORDER_MODE={risk_policy.ORDER_MODE} | "
            f"ALLOW_LIVE_ORDERS={risk_policy.ALLOW_LIVE_ORDERS} | "
            f"live orders possible: {live_possible}"
        )
        checks.append(
            CheckResult(
                "paper_live_confirmation",
                not live_possible,  # this build must never be able to go live
                mode_banner,
            )
        )

        # 10. Kill-switch self-test: trip and clear a real switch, watch the epoch.
        try:
            epoch_before = self.panel.halt_epoch
            self.panel.activate("new_entry_halt", reason="startup kill-switch self-test")
            tripped = self.panel.is_active("new_entry_halt")
            blocks = "new_entry_halt" in self.panel.blocks_new_entries()
            self.panel.clear("new_entry_halt", resumed_by="startup-self-test")
            cleared = not self.panel.is_active("new_entry_halt")
            epoch_ok = self.panel.halt_epoch == epoch_before + 2
            if tripped and blocks and cleared and epoch_ok:
                checks.append(
                    CheckResult(
                        "kill_switch_self_test",
                        True,
                        f"trip+clear verified; epoch {epoch_before} -> {self.panel.halt_epoch}",
                    )
                )
            else:
                checks.append(
                    CheckResult(
                        "kill_switch_self_test",
                        False,
                        f"trip={tripped} blocks={blocks} cleared={cleared} epoch_ok={epoch_ok}",
                    )
                )
        except Exception as exc:  # noqa: BLE001
            checks.append(CheckResult("kill_switch_self_test", False, str(exc)))

        report_obj = StartupValidationReport(
            started_at=now,
            checks=tuple(checks),
            mode_banner=mode_banner,
            broker_positions=broker_positions,
            open_orders=open_orders,
        )
        self._journal(report_obj)
        return report_obj

    def _journal(self, report: StartupValidationReport) -> None:
        self.conn.execute(
            """INSERT INTO system_events
               (id, created_at, severity, component, event_type, correlation_id, payload)
               VALUES (%s, %s, %s, 'startup', 'startup_validation', NULL, %s)""",
            (
                uuid.uuid4(),
                report.started_at,
                "info" if report.passed else "critical",
                Jsonb(
                    {
                        "passed": report.passed,
                        "mode_banner": report.mode_banner,
                        "broker_positions": report.broker_positions,
                        "open_orders": report.open_orders,
                        "checks": [
                            {"name": c.name, "passed": c.passed, "detail": c.detail}
                            for c in report.checks
                        ],
                    }
                ),
            ),
        )
