"""Shadow-config evaluation (Section 13.4 step 3): decisions on paper only.

A shadow config re-scores the SAME future opportunities the control (active)
config sees, using its own tunable weights, and records what it *would* have
done. Structurally it can do nothing else: this module — like the whole
learning package — has no import path to the trade gate, the submitter, or
any broker, and a :class:`ShadowDecision` is inert data that no execution
component accepts. A shadow config can never produce a live or paper order.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from psycopg.types.json import Jsonb

from src.config.risk_policy import MIN_OPPORTUNITY_SCORE
from src.config.tunables import TunableParams
from src.domain.values import DomainValidationError, require_money
from src.persistence.db import Connection

D = Decimal

#: The nine Section 5.8 score components (TunableParams weight_* fields minus
#: the prefix). Every candidate must carry exactly these, each in [0, 1].
SCORE_COMPONENTS: tuple[str, ...] = (
    "directional_edge",
    "gamma_efficiency",
    "theta_efficiency",
    "volatility_fit",
    "liquidity_execution",
    "catalyst_quality",
    "market_regime_fit",
    "portfolio_fit",
    "expected_value",
)


@dataclass(frozen=True)
class ShadowCandidate:
    """A data-only view of one opportunity both configs will score."""

    candidate_id: uuid.UUID
    components: Mapping[str, Decimal]  # component -> quality in [0, 1]

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, uuid.UUID):
            raise DomainValidationError("candidate_id must be a UUID")
        keys = set(self.components)
        if keys != set(SCORE_COMPONENTS):
            raise DomainValidationError(
                f"components must be exactly {sorted(SCORE_COMPONENTS)}, got {sorted(keys)}"
            )
        for name, value in self.components.items():
            dec = require_money(f"components[{name}]", value)
            if not D("0") <= dec <= D("1"):
                raise DomainValidationError(f"components[{name}] must be in [0, 1], got {dec}")


@dataclass(frozen=True)
class ShadowDecision:
    """What one config would have done. Inert data — nothing executes it."""

    candidate_id: uuid.UUID
    config_version_id: uuid.UUID
    score: Decimal
    would_enter: bool


@dataclass(frozen=True)
class ShadowEvaluator:
    """Scores candidates under one config version's tunable weights."""

    config_version_id: uuid.UUID
    params: TunableParams

    def evaluate(self, candidate: ShadowCandidate) -> ShadowDecision:
        score = D("0")
        for component in SCORE_COMPONENTS:
            weight = D(str(getattr(self.params, f"weight_{component}")))
            score += weight * D(candidate.components[component])
        score = score.quantize(D("0.0001"))
        return ShadowDecision(
            candidate_id=candidate.candidate_id,
            config_version_id=self.config_version_id,
            score=score,
            would_enter=score >= D(str(MIN_OPPORTUNITY_SCORE)),
        )


def record_shadow_decision(
    conn: Connection,
    decision: ShadowDecision,
    *,
    window_start: datetime,
    window_end: datetime,
) -> uuid.UUID:
    """Persist a shadow decision to calibration_results (sample of one)."""
    row_id = uuid.uuid4()
    conn.execute(
        """INSERT INTO calibration_results
           (id, dimension_key, window_start, window_end, sample_size, metrics,
            proposed_action)
           VALUES (%s, %s, %s, %s, 1, %s, NULL)""",
        (
            row_id,
            Jsonb(
                {
                    "type": "shadow_evaluation",
                    "config_version_id": str(decision.config_version_id),
                }
            ),
            window_start,
            window_end,
            Jsonb(
                {
                    "candidate_id": str(decision.candidate_id),
                    "score": str(decision.score),
                    "would_enter": decision.would_enter,
                    "recorded_at": datetime.now(UTC).isoformat(),
                }
            ),
        ),
    )
    return row_id
