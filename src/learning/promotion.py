"""The Section 13.4 promotion process, end to end, in code.

1. The Auditor proposes a bounded change with evidence — re-gated here: the
   parameter must be a pre-approved tunable (guardrails refused AGAIN, after
   the schema already refused them), every supporting bucket must clear the
   hard minimum sample size, and the resulting parameter set must sit inside
   the pre-approved clamp ranges.
2. A new immutable config version is created in ``shadow`` state, its
   parameters integrity-hashed into the evidence.
3. Shadow evaluation happens in :mod:`src.learning.shadow` — never an order.
4. :func:`compare_to_control` applies minimum sample size, minimum time
   window, after-cost outcomes, and a statistical-uncertainty gate
   (difference must exceed twice its standard error).
5.-6. :func:`promote` demands a passing, favorable comparison AND a named
   human approver; :func:`reject_shadow` records the rejection.
7. :func:`rollback_active` retires the current active version, restoring the
   previous one. Everything is journaled to ``system_events``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from src.agents.schemas import CalibrationReport
from src.config.risk_policy import GUARDRAIL_NAMES
from src.config.tunables import TUNABLE_NAMES, TunableParams
from src.domain.values import DomainValidationError
from src.learning.calibration import MIN_SAMPLE_SIZE
from src.orchestration.config_integrity import stamped_evidence
from src.persistence.db import Connection
from src.persistence.repositories import ConfigVersionRepository

D = Decimal

#: Section 13.4 step-4 defaults: gates a comparison must clear before a human
#: is even asked to review.
MIN_COMPARISON_SAMPLE = 30
MIN_COMPARISON_WINDOW_DAYS = 10


class PromotionError(RuntimeError):
    """The promotion process refused an illegal or under-evidenced step."""


def assert_tunable_allowed(parameter: str) -> None:
    """The calibration layer's own guardrail re-check (defense in depth after
    the TunableProposal schema): hard guardrails are untouchable, full stop."""
    if parameter in GUARDRAIL_NAMES:
        raise PromotionError(
            f"{parameter!r} is a hard guardrail; the calibration layer refuses to touch it"
        )
    if parameter not in TUNABLE_NAMES:
        raise PromotionError(f"{parameter!r} is not a pre-approved tunable parameter")


def propose_change(
    conn: Connection,
    report: CalibrationReport,
    *,
    base_params: TunableParams,
    proposed_by: str = "performance_auditor",
    min_sample_size: int = MIN_SAMPLE_SIZE,
) -> uuid.UUID | None:
    """Steps 1-2: turn an Auditor report into an immutable SHADOW version.

    Returns None when the report proposes nothing (an explicitly valid
    outcome). Raises :class:`PromotionError` on any gate violation — a
    below-minimum-sample bucket can never generate a promotion proposal."""
    if not report.proposals:
        return None

    for proposal in report.proposals:
        assert_tunable_allowed(proposal.parameter)
        if proposal.sample_size < min_sample_size:
            raise PromotionError(
                f"proposal for {proposal.parameter!r} rests on {proposal.sample_size} "
                f"samples < minimum {min_sample_size}; a loss is not automatically an "
                "error and nothing is tuned from small samples"
            )

    new_params_dict: dict[str, Any] = base_params.to_dict()
    for proposal in report.proposals:
        new_params_dict[proposal.parameter] = proposal.proposed_value
    candidate = TunableParams.from_dict(new_params_dict)
    if candidate.clamp_to_ranges() != candidate:
        raise PromotionError(
            "proposed values fall outside the pre-approved clamp ranges; bounded changes only"
        )

    evidence = stamped_evidence(
        new_params_dict,
        {
            "proposals": [
                {
                    "parameter": p.parameter,
                    "proposed_value": p.proposed_value,
                    "evidence_summary": p.evidence_summary,
                    "sample_size": p.sample_size,
                }
                for p in report.proposals
            ],
            "findings": list(report.findings),
        },
    )
    version_id = ConfigVersionRepository(conn).insert_version(
        new_params_dict, status="shadow", proposed_by=proposed_by, evidence=evidence
    )
    _journal(
        conn,
        "shadow_config_created",
        {
            "config_version_id": str(version_id),
            "proposed_by": proposed_by,
            "parameters_changed": [p.parameter for p in report.proposals],
        },
    )
    return version_id


@dataclass(frozen=True)
class ComparisonResult:
    """Shadow vs. control over the same window, after costs."""

    shadow_n: int
    control_n: int
    shadow_expectancy: Decimal
    control_expectancy: Decimal
    difference: Decimal  # shadow - control
    standard_error: Decimal | None  # of the difference; None if degenerate
    window_days: int
    eligible_for_review: bool
    favorable: bool
    significant: bool
    reasons: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "shadow_n": self.shadow_n,
            "control_n": self.control_n,
            "shadow_expectancy": str(self.shadow_expectancy),
            "control_expectancy": str(self.control_expectancy),
            "difference": str(self.difference),
            "standard_error": None if self.standard_error is None else str(self.standard_error),
            "window_days": self.window_days,
            "eligible_for_review": self.eligible_for_review,
            "favorable": self.favorable,
            "significant": self.significant,
            "reasons": list(self.reasons),
        }


def _mean_var(values: Sequence[Decimal]) -> tuple[Decimal, Decimal]:
    mean = sum(values, D("0")) / len(values)
    if len(values) < 2:
        return mean, D("0")
    var = sum(((v - mean) ** 2 for v in values), D("0")) / (len(values) - 1)
    return mean, var


def compare_to_control(
    shadow_outcomes_after_costs: Sequence[Decimal],
    control_outcomes_after_costs: Sequence[Decimal],
    *,
    window_days: int,
    min_sample_size: int = MIN_COMPARISON_SAMPLE,
    min_window_days: int = MIN_COMPARISON_WINDOW_DAYS,
) -> ComparisonResult:
    """Step 4: minimum sample, minimum window, and statistical uncertainty."""
    if not shadow_outcomes_after_costs or not control_outcomes_after_costs:
        raise DomainValidationError("both outcome samples must be non-empty")
    shadow_mean, shadow_var = _mean_var(shadow_outcomes_after_costs)
    control_mean, control_var = _mean_var(control_outcomes_after_costs)
    n_s, n_c = len(shadow_outcomes_after_costs), len(control_outcomes_after_costs)
    difference = shadow_mean - control_mean
    se_squared = shadow_var / n_s + control_var / n_c
    standard_error = se_squared.sqrt() if se_squared > 0 else None

    reasons: list[str] = []
    if n_s < min_sample_size:
        reasons.append(f"shadow sample {n_s} < minimum {min_sample_size}")
    if n_c < min_sample_size:
        reasons.append(f"control sample {n_c} < minimum {min_sample_size}")
    if window_days < min_window_days:
        reasons.append(f"window {window_days}d < minimum {min_window_days}d")
    significant = standard_error is not None and abs(difference) > 2 * standard_error
    if not significant:
        reasons.append("difference within statistical uncertainty (|diff| <= 2*SE)")

    return ComparisonResult(
        shadow_n=n_s,
        control_n=n_c,
        shadow_expectancy=shadow_mean.quantize(D("0.01")),
        control_expectancy=control_mean.quantize(D("0.01")),
        difference=difference.quantize(D("0.01")),
        standard_error=None if standard_error is None else standard_error.quantize(D("0.0001")),
        window_days=window_days,
        eligible_for_review=not reasons,
        favorable=difference > 0,
        significant=significant,
        reasons=tuple(reasons),
    )


def promote(
    conn: Connection,
    version_id: uuid.UUID,
    *,
    comparison: ComparisonResult,
    approved_by: str,
) -> None:
    """Steps 5-6: a named human promotes a shadow that beat control cleanly."""
    if not comparison.eligible_for_review:
        raise PromotionError(
            "comparison has not cleared its gates: " + "; ".join(comparison.reasons)
        )
    if not comparison.favorable:
        raise PromotionError("shadow did not outperform control; nothing to promote")
    if not approved_by or not isinstance(approved_by, str):
        raise PromotionError("promotion requires a named human approver")
    ConfigVersionRepository(conn).transition(version_id, "active", approved_by=approved_by)
    _journal(
        conn,
        "config_promoted",
        {
            "config_version_id": str(version_id),
            "approved_by": approved_by,
            "comparison": comparison.to_json(),
        },
    )


def reject_shadow(
    conn: Connection,
    version_id: uuid.UUID,
    *,
    reason: str,
    comparison: ComparisonResult | None = None,
) -> None:
    """Step 6, the other branch: rejection is logged, not silent."""
    if not reason:
        raise PromotionError("rejection requires a reason")
    ConfigVersionRepository(conn).transition(version_id, "rejected")
    _journal(
        conn,
        "config_rejected",
        {
            "config_version_id": str(version_id),
            "reason": reason,
            "comparison": None if comparison is None else comparison.to_json(),
        },
    )


def rollback_active(conn: Connection, *, rolled_back_by: str, reason: str) -> uuid.UUID:
    """Step 7: retire the current active version; the previous one takes over.

    Returns the id of the version that is active after the rollback."""
    if not rolled_back_by or not reason:
        raise PromotionError("rollback requires an identified human and a reason")
    repo = ConfigVersionRepository(conn)
    actives = conn.execute(
        "SELECT id FROM strategy_config_versions WHERE status = 'active' ORDER BY created_at DESC"
    ).fetchall()
    if not actives:
        raise PromotionError("no active config version to roll back")
    if len(actives) < 2:
        raise PromotionError(
            "rollback would leave no active config; refused — the system "
            "always runs under exactly one known-good active version"
        )
    current = actives[0]
    restored = actives[1]
    repo.transition(current["id"], "rolled_back")
    _journal(
        conn,
        "config_rolled_back",
        {
            "rolled_back_version_id": str(current["id"]),
            "restored_version_id": str(restored["id"]),
            "rolled_back_by": rolled_back_by,
            "reason": reason,
        },
    )
    return uuid.UUID(str(restored["id"]))


def _journal(conn: Connection, event_type: str, payload: dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO system_events
           (id, created_at, severity, component, event_type, correlation_id, payload)
           VALUES (%s, %s, 'info', 'promotion', %s, NULL, %s)""",
        (uuid.uuid4(), datetime.now(UTC), event_type, Jsonb(payload)),
    )
