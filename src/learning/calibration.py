"""Calibration runs: bucket every dimension, persist results, gate evidence.

Section 13.1 is enforced here as a number, not a sentiment:
``MIN_SAMPLE_SIZE`` gates which buckets may ever become evidence for the
Performance Auditor. Under-sampled buckets are still *measured and persisted*
(the operator can watch them fill up) but they are never handed to the agent
as actionable evidence and can never support a promotion proposal.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from psycopg.types.json import Jsonb

from src.agents.performance_auditor import BucketStat
from src.domain.values import DomainValidationError, require_utc
from src.learning.buckets import DIMENSION_NAMES, group_by_dimension
from src.learning.metrics import BucketMetrics, compute_metrics
from src.learning.records import TradeRecord
from src.persistence.db import Connection

#: Section 13.1 hard floor: below this, a bucket is data, never evidence.
MIN_SAMPLE_SIZE = 30


@dataclass(frozen=True)
class CalibrationBucket:
    dimension: str
    bucket: str
    metrics: BucketMetrics
    qualified: bool  # sample_size >= the hard minimum

    @property
    def sample_size(self) -> int:
        return self.metrics.sample_size


def run_calibration(
    conn: Connection | None,
    records: Sequence[TradeRecord],
    *,
    window_start: datetime,
    window_end: datetime,
    min_sample_size: int = MIN_SAMPLE_SIZE,
) -> tuple[CalibrationBucket, ...]:
    """Compute every dimension's buckets and persist one row per bucket."""
    window_start = require_utc("window_start", window_start)
    window_end = require_utc("window_end", window_end)
    if window_end < window_start:
        raise DomainValidationError("window_end before window_start")
    if min_sample_size < 1:
        raise DomainValidationError("min_sample_size must be >= 1")

    buckets: list[CalibrationBucket] = []
    for dimension in DIMENSION_NAMES:
        for bucket_name, bucket_records in sorted(group_by_dimension(records, dimension).items()):
            metrics = compute_metrics(bucket_records)
            calibration_bucket = CalibrationBucket(
                dimension=dimension,
                bucket=bucket_name,
                metrics=metrics,
                qualified=metrics.sample_size >= min_sample_size,
            )
            buckets.append(calibration_bucket)
            if conn is not None:
                conn.execute(
                    """INSERT INTO calibration_results
                       (id, dimension_key, window_start, window_end, sample_size,
                        metrics, proposed_action)
                       VALUES (%s, %s, %s, %s, %s, %s, NULL)""",
                    (
                        uuid.uuid4(),
                        Jsonb(
                            {
                                "dimension": dimension,
                                "bucket": bucket_name,
                                "qualified": calibration_bucket.qualified,
                            }
                        ),
                        window_start,
                        window_end,
                        metrics.sample_size,
                        Jsonb(metrics.to_json()),
                    ),
                )
    return tuple(buckets)


def evidence_for_auditor(
    buckets: Sequence[CalibrationBucket], *, min_sample_size: int = MIN_SAMPLE_SIZE
) -> tuple[BucketStat, ...]:
    """Only qualified buckets become the Performance Auditor's input packet.

    An under-sampled bucket never reaches the agent at all — the first of
    three layers (this filter, the agent's own offline gate, and the
    promotion layer's re-check) enforcing 'no tuning from individual
    outcomes'."""
    stats: list[BucketStat] = []
    for b in buckets:
        if b.metrics.sample_size < min_sample_size:
            continue
        stats.append(
            BucketStat(
                dimension=f"{b.dimension}={b.bucket}",
                sample_size=b.metrics.sample_size,
                win_rate=b.metrics.win_rate,
                expectancy_after_costs=b.metrics.expectancy_after_costs,
                avg_slippage=b.metrics.avg_slippage_vs_mid,
            )
        )
    return tuple(stats)
