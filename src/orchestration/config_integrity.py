"""Tamper-evident hashing for strategy config versions (Sections 15.1/17).

The active config's ``parameters`` are hashed (SHA-256 over canonical JSON)
and the hash stored in the version's ``evidence`` at insert time. Startup
validation recomputes and compares: a missing or mismatching hash blocks the
session. The parameters column is already immutable at the database level
(migration 0004); the hash makes tampering *evident*, not merely forbidden.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

HASH_KEY = "parameters_sha256"


def parameters_hash(parameters: dict[str, Any]) -> str:
    canonical = json.dumps(parameters, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def stamped_evidence(
    parameters: dict[str, Any], evidence: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Evidence dict carrying the integrity hash — use at insert time."""
    return {**(evidence or {}), HASH_KEY: parameters_hash(parameters)}


def verify_config_row(row: dict[str, Any]) -> tuple[bool, str]:
    """Check a strategy_config_versions row's integrity hash."""
    evidence = row.get("evidence") or {}
    recorded = evidence.get(HASH_KEY)
    if not recorded:
        return False, "active config has no integrity hash in evidence"
    actual = parameters_hash(dict(row["parameters"]))
    if actual != recorded:
        return False, f"config hash mismatch: recorded {recorded[:12]}..., actual {actual[:12]}..."
    return True, f"hash verified ({actual[:12]}...)"
