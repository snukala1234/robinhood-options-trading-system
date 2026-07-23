"""Tests for Agent 7 (exit monitor): pure-code stops first, thesis + outage fallback."""

from __future__ import annotations

from types import SimpleNamespace

from agents.exit_monitor import (
    ACTION_EXIT,
    ACTION_HOLD,
    REASON_STOP_LOSS,
    REASON_TAKE_PROFIT,
    REASON_THESIS,
    ExitMonitor,
)
from config.guardrails import HARD_STOP_LOSS_PCT
from core.llm import AllModelsUnavailableError
from core.records import Position


class FakeClient:
    """Minimal LLM client: returns fixed data, or simulates a total model outage."""

    def __init__(self, data: dict | None = None, raise_outage: bool = False) -> None:
        self._data = data if data is not None else {"invalidated": False}
        self._raise = raise_outage

    def complete_json(self, agent_key, system, user, offline, agent_name=None):  # noqa: ANN001
        if self._raise:
            raise AllModelsUnavailableError("total outage")
        return SimpleNamespace(
            data=self._data, active_model="claude-fable-5", decided_under_failover=False
        )


def position(entry: float = 100.0, tp: float = 0.25) -> Position:
    return Position(
        "t1", "AAPL", entry, 1.0, entry, "2026-07-05T00:00:00+00:00", HARD_STOP_LOSS_PCT, tp
    )


def test_forced_stop_loss_exit() -> None:
    mon = ExitMonitor(client=FakeClient())
    at_stop = 100.0 * (1 - HARD_STOP_LOSS_PCT)
    d = mon.evaluate_position(position(), current_price=at_stop)
    assert d.action == ACTION_EXIT and d.reason == REASON_STOP_LOSS and d.forced is True


def test_take_profit_exit() -> None:
    mon = ExitMonitor(client=FakeClient())
    d = mon.evaluate_position(position(tp=0.25), current_price=130.0)  # +30% > 25%
    assert d.action == ACTION_EXIT and d.reason == REASON_TAKE_PROFIT and d.forced is False


def test_hold_within_band_thesis_intact() -> None:
    mon = ExitMonitor(client=FakeClient({"invalidated": False}))
    d = mon.evaluate_position(position(), current_price=105.0)
    assert d.action == ACTION_HOLD and d.thesis_checked is True


def test_thesis_invalidation_exit() -> None:
    mon = ExitMonitor(client=FakeClient({"invalidated": True, "reason": "guidance cut"}))
    d = mon.evaluate_position(position(), current_price=105.0)
    assert d.action == ACTION_EXIT and d.reason == REASON_THESIS


def test_model_outage_falls_back_to_pure_code_only() -> None:
    # No model reachable: thesis is skipped, pure-code rules still govern. Must not raise.
    mon = ExitMonitor(client=FakeClient(raise_outage=True))
    d = mon.evaluate_position(position(), current_price=105.0)
    assert d.action == ACTION_HOLD and d.thesis_checked is False


def test_model_outage_still_forces_stop_loss() -> None:
    mon = ExitMonitor(client=FakeClient(raise_outage=True))
    at_stop = 100.0 * (1 - HARD_STOP_LOSS_PCT)
    d = mon.evaluate_position(position(), current_price=at_stop)
    # Pure-code stop runs BEFORE any model call, so the outage is irrelevant.
    assert d.action == ACTION_EXIT and d.reason == REASON_STOP_LOSS and d.forced is True


def test_evaluate_all_multiple_positions() -> None:
    mon = ExitMonitor(client=FakeClient({"invalidated": False}))
    positions = [
        position(),
        Position(
            "t2", "MSFT", 200.0, 1.0, 200.0, "2026-07-05T00:00:00+00:00", HARD_STOP_LOSS_PCT, 0.25
        ),
    ]
    prices = {"AAPL": 100.0 * (1 - HARD_STOP_LOSS_PCT), "MSFT": 260.0}  # AAPL stop, MSFT TP
    decisions = mon.evaluate_all(positions, price_of=lambda s: prices[s])
    reasons = {d.position.symbol: (d.action, d.reason) for d in decisions}
    assert reasons["AAPL"] == (ACTION_EXIT, REASON_STOP_LOSS)
    assert reasons["MSFT"] == (ACTION_EXIT, REASON_TAKE_PROFIT)
