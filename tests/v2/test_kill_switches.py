"""Kill-switch panel: monotonic halt epoch, manual resume, entry blocking."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from src.config.risk_policy import KILL_SWITCHES
from src.gate.kill_switches import (
    CIRCUIT_BREAKER_NAMES,
    KillSwitchPanel,
    ManualResumeRequired,
    UnknownSwitchError,
)


def test_epoch_starts_at_zero_and_activation_bumps_it() -> None:
    panel = KillSwitchPanel()
    assert panel.halt_epoch == 0
    assert panel.activate("global_trading_halt", reason="test") == 1
    assert panel.halt_epoch == 1
    assert panel.is_active("global_trading_halt")


def test_reactivating_an_active_switch_does_not_bump_epoch() -> None:
    panel = KillSwitchPanel()
    panel.activate("drawdown_breach", reason="first")
    assert panel.activate("drawdown_breach", reason="again") == 1
    assert panel.halt_epoch == 1


def test_clear_also_bumps_epoch_so_tokens_from_the_halted_regime_die() -> None:
    panel = KillSwitchPanel()
    panel.activate("manual_emergency_stop", reason="test")
    assert panel.clear("manual_emergency_stop", resumed_by="operator") == 2
    assert panel.halt_epoch == 2
    assert not panel.is_active("manual_emergency_stop")


def test_clear_requires_an_identified_human() -> None:
    panel = KillSwitchPanel()
    panel.activate("global_trading_halt", reason="test")
    with pytest.raises(ManualResumeRequired):
        panel.clear("global_trading_halt", resumed_by="")
    assert panel.is_active("global_trading_halt")  # still halted


def test_unknown_switch_names_are_rejected() -> None:
    panel = KillSwitchPanel()
    with pytest.raises(UnknownSwitchError):
        panel.activate("made_up_switch", reason="x")
    with pytest.raises(UnknownSwitchError):
        panel.is_active("made_up_switch")


def test_every_section_3_2_switch_and_breaker_is_accepted() -> None:
    panel = KillSwitchPanel()
    for i, name in enumerate([*KILL_SWITCHES, *CIRCUIT_BREAKER_NAMES], start=1):
        assert panel.activate(name, reason="sweep") == i
    assert len(panel.active_switches()) == len(KILL_SWITCHES) + len(CIRCUIT_BREAKER_NAMES)


def test_any_active_switch_blocks_new_entries() -> None:
    panel = KillSwitchPanel()
    assert panel.blocks_new_entries() == ()
    panel.activate("excessive_slippage", reason="test")
    assert panel.blocks_new_entries() == ("excessive_slippage",)
    # But a pure new-entry halt does not block risk-reducing exits.
    assert panel.blocks_exits() == ()
    panel.activate("manual_emergency_stop", reason="test")
    assert "manual_emergency_stop" in panel.blocks_exits()


def test_activation_requires_a_reason() -> None:
    panel = KillSwitchPanel()
    with pytest.raises(ValueError):
        panel.activate("global_trading_halt", reason="")


def test_changes_are_recorded_as_critical_system_events(
    conn: psycopg.Connection[Any],
) -> None:
    panel = KillSwitchPanel(conn=conn)
    panel.activate("broker_degradation", reason="transport errors")
    panel.clear("broker_degradation", resumed_by="operator")
    rows = conn.execute(
        "SELECT * FROM system_events WHERE component = 'kill_switches' ORDER BY created_at"
    ).fetchall()
    assert [r["event_type"] for r in rows] == ["kill_switch_activated", "kill_switch_cleared"]
    assert all(r["severity"] == "critical" for r in rows)
    assert rows[0]["payload"]["halt_epoch"] == 1
    assert rows[1]["payload"]["resumed_by"] == "operator"
