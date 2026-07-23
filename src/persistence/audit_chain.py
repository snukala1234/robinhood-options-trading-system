"""Audit-chain verification and HMAC anchoring (audit finding 3, Section 17).

Two layers of tamper evidence over the append-only event tables:

1. **The in-database hash chain** (migration 0015): every row stores
   ``sha256(prev_row_hash || canonical(row))``, stamped by trigger so every
   writer is covered. Any mutation or deletion — accidental corruption, a
   buggy migration, a bypassed trigger — breaks the arithmetic, and
   :func:`verify_table` finds the exact rows.
2. **HMAC anchors**: the chain alone is recomputable by anyone with database
   access, so periodically :func:`record_anchor` signs the chain head with a
   key held OUTSIDE the database (environment secret / OS keychain, never a
   table). An attacker who rewrites rows and recomputes the whole chain still
   cannot forge an anchor, and :func:`verify_anchors` catches the mismatch.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_lib
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from src.config.environments import ConfigurationError
from src.persistence.db import Connection

#: The append-only tables covered by the chain (migration 0015).
AUDIT_CHAIN_TABLES = ("order_events", "agent_decisions", "system_events")

#: Environment variable holding the anchor HMAC key — outside the database.
HMAC_KEY_ENV = "AUDIT_CHAIN_HMAC_KEY"


@dataclass(frozen=True)
class ChainBreak:
    table: str
    chain_seq: int
    detail: str


@dataclass(frozen=True)
class ChainReport:
    table: str
    rows: int
    breaks: tuple[ChainBreak, ...]

    @property
    def intact(self) -> bool:
        return not self.breaks


def hmac_key() -> bytes:
    """The anchor key, from the environment only (Section 17). No default."""
    key = os.environ.get(HMAC_KEY_ENV, "")
    if not key:
        raise ConfigurationError(
            f"{HMAC_KEY_ENV} is not set; the audit-chain anchor key must come "
            "from the environment or OS keychain, never the database"
        )
    return key.encode("utf-8")


def verify_table(conn: Connection, table: str) -> ChainReport:
    """Recompute the whole chain server-side; report every broken row."""
    if table not in AUDIT_CHAIN_TABLES:
        raise ValueError(f"{table!r} is not an audit-chained table")
    count_row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
    assert count_row is not None
    rows = int(count_row["n"])
    breaks: list[ChainBreak] = []
    for row in conn.execute("SELECT * FROM audit_chain_breaks(%s)", (table,)).fetchall():
        details: list[str] = []
        if row["recomputed_hash"] != row["stored_hash"]:
            details.append(
                f"row hash mismatch (stored {str(row['stored_hash'])[:12]}..., "
                f"recomputed {str(row['recomputed_hash'])[:12]}...)"
            )
        if row["expected_prev"] != row["stored_prev"]:
            details.append("previous-hash linkage broken (row inserted, removed, or reordered)")
        breaks.append(
            ChainBreak(table, int(row["chain_seq"]), "; ".join(details) or "unknown break")
        )
    return ChainReport(table=table, rows=rows, breaks=tuple(breaks))


def verify_all(conn: Connection) -> dict[str, ChainReport]:
    return {table: verify_table(conn, table) for table in AUDIT_CHAIN_TABLES}


def chain_head(conn: Connection, table: str) -> tuple[int, str] | None:
    if table not in AUDIT_CHAIN_TABLES:
        raise ValueError(f"{table!r} is not an audit-chained table")
    row = conn.execute(
        f"SELECT chain_seq, row_hash FROM {table} ORDER BY chain_seq DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return int(row["chain_seq"]), str(row["row_hash"])


def _anchor_mac(key: bytes, table: str, chain_seq: int, head_hash: str) -> str:
    message = f"{table}|{chain_seq}|{head_hash}".encode()
    return hmac_lib.new(key, message, hashlib.sha256).hexdigest()


def record_anchor(conn: Connection, table: str, *, key: bytes | None = None) -> uuid.UUID:
    """Sign the current chain head with the external key and store the anchor.

    Anchors live in system_events (and thereby join its own chain); each
    anchor covers the target table's head at signing time."""
    key = key if key is not None else hmac_key()
    head = chain_head(conn, table)
    if head is None:
        raise ValueError(f"cannot anchor {table!r}: chain is empty")
    chain_seq, head_hash = head
    anchor_id = uuid.uuid4()
    conn.execute(
        """INSERT INTO system_events
           (id, created_at, severity, component, event_type, correlation_id, payload)
           VALUES (%s, %s, 'info', 'audit_chain', 'audit_chain_anchor', NULL, %s)""",
        (
            anchor_id,
            datetime.now(UTC),
            Jsonb(
                {
                    "table": table,
                    "chain_seq": chain_seq,
                    "head_hash": head_hash,
                    "hmac": _anchor_mac(key, table, chain_seq, head_hash),
                }
            ),
        ),
    )
    return anchor_id


def verify_anchors(conn: Connection, table: str, *, key: bytes | None = None) -> tuple[str, ...]:
    """Check every stored anchor for the table against the external key and
    against the live chain. Returns problems; empty means all anchors hold."""
    key = key if key is not None else hmac_key()
    problems: list[str] = []
    anchors = conn.execute(
        """SELECT id, payload FROM system_events
           WHERE event_type = 'audit_chain_anchor' AND payload->>'table' = %s
           ORDER BY created_at""",
        (table,),
    ).fetchall()
    for anchor in anchors:
        payload = anchor["payload"]
        chain_seq = int(payload["chain_seq"])
        head_hash = str(payload["head_hash"])
        expected = _anchor_mac(key, table, chain_seq, head_hash)
        if not hmac_lib.compare_digest(expected, str(payload["hmac"])):
            problems.append(f"anchor {anchor['id']}: HMAC invalid (forged or wrong key)")
            continue
        row = conn.execute(
            f"SELECT row_hash FROM {table} WHERE chain_seq = %s", (chain_seq,)
        ).fetchone()
        if row is None:
            problems.append(
                f"anchor {anchor['id']}: anchored row chain_seq={chain_seq} is missing "
                "(history truncated?)"
            )
        elif str(row["row_hash"]) != head_hash:
            problems.append(
                f"anchor {anchor['id']}: anchored row chain_seq={chain_seq} hash changed"
            )
    return tuple(problems)
