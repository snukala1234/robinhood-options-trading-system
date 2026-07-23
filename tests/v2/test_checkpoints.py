"""DTE and assignment-risk checkpoints: the escalation ladder is monotonic."""

from __future__ import annotations

import pytest

from src.domain.values import DomainValidationError
from src.positions.checkpoints import CheckpointLevel, dte_checkpoint


def _level(dte: int, *, short_itm: bool = False, notice: bool = False) -> CheckpointLevel:
    return dte_checkpoint(dte, short_leg_itm=short_itm, assignment_notice=notice).level


def test_far_from_expiration_no_checkpoint() -> None:
    result = dte_checkpoint(10, short_leg_itm=False, assignment_notice=False)
    assert result.level is CheckpointLevel.NONE
    assert result.required_action == "none"
    assert result.reasons == ()


def test_review_checkpoint_at_five_dte() -> None:
    result = dte_checkpoint(5, short_leg_itm=False, assignment_notice=False)
    assert result.level is CheckpointLevel.DTE_REVIEW
    assert result.required_action == "mandatory_review"


def test_short_leg_itm_escalates_the_review_window() -> None:
    result = dte_checkpoint(5, short_leg_itm=True, assignment_notice=False)
    assert result.level is CheckpointLevel.ASSIGNMENT_WATCH
    assert result.required_action == "review_and_prepare_exit"


def test_forced_exit_at_two_dte() -> None:
    result = dte_checkpoint(2, short_leg_itm=False, assignment_notice=False)
    assert result.level is CheckpointLevel.FORCED_EXIT
    assert result.required_action == "exit_before_expiration"


def test_short_leg_itm_in_forced_window_is_emergency() -> None:
    result = dte_checkpoint(1, short_leg_itm=True, assignment_notice=False)
    assert result.level is CheckpointLevel.EMERGENCY
    assert result.required_action == "risk_reducing_exit_now"


def test_assignment_notice_is_emergency_at_any_dte() -> None:
    result = dte_checkpoint(20, short_leg_itm=False, assignment_notice=True)
    assert result.level is CheckpointLevel.EMERGENCY


@pytest.mark.parametrize("short_itm", [False, True])
def test_escalation_is_monotonic_as_expiration_approaches(short_itm: bool) -> None:
    levels = [_level(dte, short_itm=short_itm) for dte in range(0, 15)]
    # dte ascending -> level must never increase (i.e. it only escalates as dte falls)
    assert all(levels[i] >= levels[i + 1] for i in range(len(levels) - 1))


def test_negative_dte_rejected() -> None:
    with pytest.raises(DomainValidationError):
        dte_checkpoint(-1, short_leg_itm=False, assignment_notice=False)
