"""Degraded mode: no new entries, risk-reducing exits still possible.

This wires Section 3.2's core rule ("uncertain state means no new entries")
across the layers that each enforce a piece of it:

- Phase D reconciliation: :func:`new_entries_allowed` blocks entries while any
  order is uncertain or stale-submitted.
- Phase F kill-switch panel: every active switch blocks new entries; only the
  exit-blocking subset (global halt, manual emergency stop, broker
  degradation) stops risk-reducing exits.
- Phase G exit path: settlement never blocks a closing order
  (:func:`~src.risk.settlement.closing_order_cash_check`), and the token-free
  :meth:`~src.execution.submission.OrderSubmitter.submit_exit` honors exactly
  the exit-blocking subset.

The asymmetry is the point: a degraded system can only get safer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.execution.order_state_machine import OrderStateMachine
from src.execution.reconciliation import new_entries_allowed
from src.gate.kill_switches import KillSwitchPanel


@dataclass(frozen=True)
class DegradedModeStatus:
    new_entries_allowed: bool
    entry_block_reasons: tuple[str, ...]
    exits_allowed: bool
    exit_block_reasons: tuple[str, ...]

    @property
    def degraded(self) -> bool:
        return not self.new_entries_allowed


def degraded_mode_status(
    machine: OrderStateMachine, panel: KillSwitchPanel, *, now: datetime
) -> DegradedModeStatus:
    """One combined view of what is currently allowed and why not."""
    entries_ok, reconciliation_reasons = new_entries_allowed(machine, now=now)
    entry_reasons = list(reconciliation_reasons)
    entry_reasons.extend(f"kill switch active: {name}" for name in panel.blocks_new_entries())
    exit_reasons = [f"kill switch active: {name}" for name in panel.blocks_exits()]
    return DegradedModeStatus(
        new_entries_allowed=entries_ok and not entry_reasons,
        entry_block_reasons=tuple(entry_reasons),
        exits_allowed=not exit_reasons,
        exit_block_reasons=tuple(exit_reasons),
    )
