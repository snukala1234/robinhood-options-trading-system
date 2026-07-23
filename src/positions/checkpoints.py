"""DTE and assignment-risk checkpoints that escalate as expiration approaches.

The ladder (thresholds from the tunables, defaults 5-day review / 2-day
forced exit):

- ``NONE``             dte above the review checkpoint, no assignment danger
- ``DTE_REVIEW``       dte <= review checkpoint: mandatory review
- ``ASSIGNMENT_WATCH`` review window with a short leg in the money
- ``FORCED_EXIT``      dte <= forced-exit threshold: close by default
- ``EMERGENCY``        forced-exit window with a short leg ITM, or an actual
                       assignment/exercise notice at ANY dte

Escalation is monotonic: as dte falls (or assignment danger appears) the
level only rises. Every level maps to a required action that pure code can
execute.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from src.config.tunables import DEFAULT_TUNABLES, TunableParams
from src.domain.values import DomainValidationError


class CheckpointLevel(IntEnum):
    NONE = 0
    DTE_REVIEW = 1
    ASSIGNMENT_WATCH = 2
    FORCED_EXIT = 3
    EMERGENCY = 4


_REQUIRED_ACTIONS: dict[CheckpointLevel, str] = {
    CheckpointLevel.NONE: "none",
    CheckpointLevel.DTE_REVIEW: "mandatory_review",
    CheckpointLevel.ASSIGNMENT_WATCH: "review_and_prepare_exit",
    CheckpointLevel.FORCED_EXIT: "exit_before_expiration",
    CheckpointLevel.EMERGENCY: "risk_reducing_exit_now",
}


@dataclass(frozen=True)
class CheckpointResult:
    level: CheckpointLevel
    required_action: str
    reasons: tuple[str, ...]


def dte_checkpoint(
    dte: int,
    *,
    short_leg_itm: bool,
    assignment_notice: bool,
    tunables: TunableParams = DEFAULT_TUNABLES,
) -> CheckpointResult:
    """Resolve the escalation level for one position."""
    if not isinstance(dte, int) or isinstance(dte, bool) or dte < 0:
        raise DomainValidationError(f"dte must be a non-negative int, got {dte!r}")

    reasons: list[str] = []
    if assignment_notice:
        level = CheckpointLevel.EMERGENCY
        reasons.append("assignment/exercise notice received")
    elif dte <= tunables.dte_forced_exit:
        if short_leg_itm:
            level = CheckpointLevel.EMERGENCY
            reasons.append(
                f"short leg ITM inside the forced-exit window (dte {dte} <="
                f" {tunables.dte_forced_exit})"
            )
        else:
            level = CheckpointLevel.FORCED_EXIT
            reasons.append(f"dte {dte} <= forced-exit threshold {tunables.dte_forced_exit}")
    elif dte <= tunables.dte_review_checkpoint:
        if short_leg_itm:
            level = CheckpointLevel.ASSIGNMENT_WATCH
            reasons.append(
                f"short leg ITM inside the review window (dte {dte} <="
                f" {tunables.dte_review_checkpoint})"
            )
        else:
            level = CheckpointLevel.DTE_REVIEW
            reasons.append(f"dte {dte} <= review checkpoint {tunables.dte_review_checkpoint}")
    else:
        level = CheckpointLevel.NONE

    return CheckpointResult(level, _REQUIRED_ACTIONS[level], tuple(reasons))
