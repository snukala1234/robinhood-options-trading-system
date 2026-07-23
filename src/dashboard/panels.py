"""The seven Section 18 panels as pure read queries.

Every function takes a connection, runs SELECTs, and returns a JSON-safe
dict: Decimals and timestamps become strings (money never becomes a float,
not even for display), and every string that originated as stored free text
— reasons, theses, catalyst descriptions, event payloads — is HTML-escaped
before it leaves: data, never markup, never instructions.

No function here imports or touches the gate, the execution layer, a broker,
or an agent. The dashboard describes state; it cannot cause any.
"""

from __future__ import annotations

import datetime as dt
import html
import uuid
from decimal import Decimal
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow

from src.config import environments, risk_policy

Conn = Connection[DictRow]

#: Strategies entered for a net credit (kept in sync with the execution layer
#: by a test — the dashboard must not import from it).
_CREDIT_STRATEGIES = frozenset({"put_credit_spread", "call_credit_spread"})


def jsonify(value: Any) -> Any:
    """Recursively make DB values JSON-safe without ever minting a float."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): jsonify(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonify(v) for v in value]
    return value


def escape_tree(value: Any) -> Any:
    """HTML-escape every string leaf: stored text renders as data only."""
    if isinstance(value, str):
        return html.escape(value)
    if isinstance(value, dict):
        return {str(k): escape_tree(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [escape_tree(v) for v in value]
    return jsonify(value)


def _banner() -> dict[str, Any]:
    return {
        "mode": "PAPER" if risk_policy.PAPER_TRADING else "LIVE",
        "order_mode": risk_policy.ORDER_MODE,
        "allow_live_orders": risk_policy.ALLOW_LIVE_ORDERS,
        "live_orders_possible": environments.live_orders_permitted(),
    }


# --- Panel A: system and agent activity ---------------------------------------


def panel_system(conn: Conn) -> dict[str, Any]:
    session = conn.execute(
        "SELECT payload, created_at FROM system_events WHERE component = 'session' "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    startup = conn.execute(
        "SELECT payload, created_at FROM system_events "
        "WHERE event_type = 'startup_validation' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()

    # Reconstruct live kill-switch state from the (chained) event stream.
    active_switches: dict[str, Any] = {}
    for row in conn.execute(
        "SELECT event_type, payload FROM system_events "
        "WHERE component = 'kill_switches' ORDER BY created_at, chain_seq"
    ).fetchall():
        payload = row["payload"]
        if row["event_type"] == "kill_switch_activated":
            active_switches[str(payload["switch"])] = escape_tree(payload.get("reason", ""))
        elif row["event_type"] == "kill_switch_cleared":
            active_switches.pop(str(payload["switch"]), None)

    agents = conn.execute(
        """SELECT DISTINCT ON (agent_name)
                  agent_name, model_id, prompt_version, created_at, validation_result
           FROM agent_decisions ORDER BY agent_name, created_at DESC"""
    ).fetchall()
    decisions_today = conn.execute(
        "SELECT count(*) AS n FROM agent_decisions WHERE created_at::date = current_date"
    ).fetchone()

    caps = conn.execute(
        "SELECT observed_at, capabilities, source_version FROM broker_capability_snapshots "
        "WHERE is_current LIMIT 1"
    ).fetchone()
    freshest = conn.execute(
        "SELECT max(observed_at) AS observed_at FROM market_data_snapshots"
    ).fetchone()

    return {
        "banner": _banner(),
        "session_state": (
            escape_tree(session["payload"]).get("new_state") if session else "OFFLINE"
        ),
        "session_changed_at": jsonify(session["created_at"]) if session else None,
        "startup_validation": escape_tree(startup["payload"]) if startup else None,
        "kill_switches_active": active_switches,
        "circuit_breakers_tripped": sorted(
            name for name in active_switches if name.endswith("_breach")
        ),
        "agents": [
            {
                "agent": row["agent_name"],
                "model_id": row["model_id"],
                "prompt_version": row["prompt_version"],
                "last_decision_at": jsonify(row["created_at"]),
                "last_validation": escape_tree(row["validation_result"]),
            }
            for row in agents
        ],
        "agent_decisions_today": int(decisions_today["n"]) if decisions_today else 0,
        "broker": {
            "capabilities_observed_at": jsonify(caps["observed_at"]) if caps else None,
            "capabilities": jsonify(caps["capabilities"]) if caps else None,
            "source_version": caps["source_version"] if caps else None,
        },
        "market_data_freshest_at": jsonify(freshest["observed_at"]) if freshest else None,
    }


# --- Panel B: equity and drawdown ----------------------------------------------


def panel_equity(conn: Conn) -> dict[str, Any]:
    curve = conn.execute(
        "SELECT observed_at, total_equity, drawdown, high_water_mark "
        "FROM portfolio_snapshots ORDER BY observed_at"
    ).fetchall()
    latest = curve[-1] if curve else None
    latest_full = (
        conn.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY observed_at DESC LIMIT 1"
        ).fetchone()
        if latest
        else None
    )

    realized = Decimal("0")
    for row in conn.execute(
        "SELECT strategy, entry_net_price, exit_net_price, quantity FROM positions "
        "WHERE status = 'closed' AND exit_net_price IS NOT NULL"
    ).fetchall():
        per_share = (
            row["exit_net_price"] - row["entry_net_price"]
            if row["strategy"] not in _CREDIT_STRATEGIES
            else row["entry_net_price"] - row["exit_net_price"]
        )
        realized += per_share * 100 * row["quantity"]

    unrealized_row = conn.execute(
        """SELECT sum(s.unrealized_pnl) AS total FROM position_snapshots s
           JOIN (SELECT position_id, max(observed_at) AS observed_at
                 FROM position_snapshots GROUP BY position_id) latest
             ON s.position_id = latest.position_id
            AND s.observed_at = latest.observed_at
           JOIN positions p ON p.id = s.position_id AND p.status = 'open'"""
    ).fetchone()

    return {
        "banner": _banner(),
        "equity_curve": [jsonify(dict(r)) for r in curve],
        "latest": jsonify(dict(latest_full)) if latest_full else None,
        "realized_pnl": str(realized),
        "unrealized_pnl": (
            str(unrealized_row["total"])
            if unrealized_row and unrealized_row["total"] is not None
            else None
        ),
    }


# --- Panel C: portfolio Greeks --------------------------------------------------


def panel_greeks(conn: Conn) -> dict[str, Any]:
    snapshot = conn.execute(
        "SELECT * FROM portfolio_snapshots ORDER BY observed_at DESC LIMIT 1"
    ).fetchone()

    def _headroom(value: Decimal | None, pct_limit: float | None) -> dict[str, Any]:
        if snapshot is None or value is None:
            return {"value": None, "limit": pct_limit, "headroom": None}
        if pct_limit is None:
            return {"value": str(value), "limit": None, "headroom": None}
        cap = snapshot["total_equity"] * Decimal(str(pct_limit))
        return {
            "value": str(value),
            "limit_dollars": str(cap),
            "headroom_dollars": str(cap - abs(value)),
            "exceeded": abs(value) > cap,
        }

    exposures: dict[str, dict[str, str]] = {
        "by_underlying": {},
        "by_strategy": {},
        "by_expiration": {},
    }
    for row in conn.execute(
        "SELECT underlying, strategy, expiration, max_loss FROM positions WHERE status = 'open'"
    ).fetchall():
        for key, field in (
            ("by_underlying", row["underlying"]),
            ("by_strategy", row["strategy"]),
            ("by_expiration", row["expiration"].isoformat()),
        ):
            current = Decimal(exposures[key].get(str(field), "0"))
            exposures[key][str(field)] = str(current + row["max_loss"])

    return {
        "observed_at": jsonify(snapshot["observed_at"]) if snapshot else None,
        "net_delta": _headroom(
            snapshot["net_delta"] if snapshot else None, risk_policy.MAX_NET_ABS_DELTA_PCT
        ),
        "net_gamma": _headroom(
            snapshot["net_gamma"] if snapshot else None, risk_policy.MAX_PORTFOLIO_GAMMA
        ),
        "daily_theta": _headroom(
            snapshot["daily_theta"] if snapshot else None, risk_policy.MAX_DAILY_THETA_BURN_PCT
        ),
        "net_vega": _headroom(
            snapshot["net_vega"] if snapshot else None, risk_policy.MAX_ABS_VEGA_PCT
        ),
        "open_risk": str(snapshot["open_risk"]) if snapshot else None,
        "exposures": exposures,
        "note": "sector exposure requires per-position sector tags (not persisted in v2 schema)",
    }


# --- Panel D: opportunity board -------------------------------------------------


def panel_opportunities(conn: Conn) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM opportunity_candidates ORDER BY total_score DESC, created_at DESC LIMIT 50"
    ).fetchall()
    return {
        "candidates": [
            {
                "id": str(row["id"]),
                "created_at": jsonify(row["created_at"]),
                "underlying": row["underlying"],
                "strategy": row["strategy"],
                "expiration": jsonify(row["expiration"]),
                "total_score": str(row["total_score"]),
                "score_components": jsonify(row["score_components"]),
                "analytics": escape_tree(row["analytics"]),
                "legs": jsonify(row["legs"]),
                "status": html.escape(str(row["status"])),
                "rejection_reasons": escape_tree(row["rejection_reasons"] or []),
            }
            for row in rows
        ],
        "note": "read-only: there are no action controls",
    }


# --- Panel E: open positions ----------------------------------------------------


def panel_positions(conn: Conn) -> dict[str, Any]:
    rows = conn.execute(
        """SELECT p.*, tp.proposal AS proposal_doc, tp.risk_decision
           FROM positions p
           LEFT JOIN trade_proposals tp ON tp.id = p.proposal_id
           WHERE p.status = 'open' ORDER BY p.opened_at"""
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        snapshot = conn.execute(
            "SELECT * FROM position_snapshots WHERE position_id = %s "
            "ORDER BY observed_at DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        out.append(
            {
                "id": str(row["id"]),
                "underlying": row["underlying"],
                "strategy": row["strategy"],
                "expiration": jsonify(row["expiration"]),
                "legs": jsonify(row["legs"]),
                "opened_at": jsonify(row["opened_at"]),
                "entry_net_price": str(row["entry_net_price"]),
                "quantity": row["quantity"],
                "remaining_max_risk": str(row["max_loss"]),
                "exit_plan": escape_tree(row["exit_plan"]),
                "proposal": escape_tree(row["proposal_doc"]) if row["proposal_doc"] else None,
                "risk_decision": (
                    escape_tree(row["risk_decision"]) if row["risk_decision"] else None
                ),
                "snapshot": (
                    {
                        "observed_at": jsonify(snapshot["observed_at"]),
                        "marked_value": jsonify(snapshot["marked_value"]),
                        "unrealized_pnl": jsonify(snapshot["unrealized_pnl"]),
                        "net_delta": jsonify(snapshot["net_delta"]),
                        "net_gamma": jsonify(snapshot["net_gamma"]),
                        "net_theta": jsonify(snapshot["net_theta"]),
                        "net_vega": jsonify(snapshot["net_vega"]),
                        "dte": snapshot["dte"],
                        "liquidity": escape_tree(snapshot["liquidity"] or {}),
                        "thesis_state": escape_tree(snapshot["thesis_state"] or {}),
                    }
                    if snapshot
                    else None
                ),
            }
        )
    return {"open_positions": out}


# --- Panel F: orders and reconciliation -----------------------------------------


def panel_orders(conn: Conn) -> dict[str, Any]:
    orders = conn.execute(
        "SELECT * FROM orders ORDER BY submitted_at DESC NULLS LAST LIMIT 100"
    ).fetchall()
    order_docs: list[dict[str, Any]] = []
    for row in orders:
        raw_request = row["raw_request"] or {}
        fills = conn.execute(
            """SELECT broker_payload FROM order_events
               WHERE order_id = %s AND broker_payload ? 'avg_fill_price'
               ORDER BY event_at DESC LIMIT 1""",
            (row["id"],),
        ).fetchone()
        avg_fill = fills["broker_payload"].get("avg_fill_price") if fills is not None else None
        midpoint = raw_request.get("structure_midpoint")
        limit_price = raw_request.get("limit_price")
        slippage: dict[str, Any] | None = None
        if avg_fill is not None:
            basis_value = midpoint if midpoint is not None else limit_price
            if basis_value is not None:
                slippage = {
                    "basis": "midpoint" if midpoint is not None else "limit_price",
                    "basis_value": str(basis_value),
                    "avg_fill_price": str(avg_fill),
                    "difference": str(Decimal(str(avg_fill)) - Decimal(str(basis_value))),
                }
        order_docs.append(
            {
                "id": str(row["id"]),
                "state": row["current_state"],
                "idempotency_key": html.escape(str(row["idempotency_key"])),
                "broker_order_id": row["broker_order_id"],
                "submitted_at": jsonify(row["submitted_at"]),
                "request": escape_tree(raw_request),
                "slippage": slippage,
            }
        )

    reconciliation_required = conn.execute(
        "SELECT count(*) AS n FROM orders WHERE current_state = 'RECONCILIATION_REQUIRED'"
    ).fetchone()
    recent_flags = conn.execute(
        """SELECT created_at, event_type, payload FROM system_events
           WHERE component = 'reconciliation' ORDER BY created_at DESC LIMIT 20"""
    ).fetchall()

    return {
        "orders": order_docs,
        "reconciliation": {
            "orders_requiring_reconciliation": (
                int(reconciliation_required["n"]) if reconciliation_required else 0
            ),
            "recent_warnings": [
                {
                    "at": jsonify(r["created_at"]),
                    "event_type": r["event_type"],
                    "payload": escape_tree(r["payload"]),
                }
                for r in recent_flags
            ],
        },
    }


# --- Panel G: performance and calibration ---------------------------------------


def panel_performance(conn: Conn) -> dict[str, Any]:
    buckets = conn.execute(
        """SELECT * FROM calibration_results
           WHERE dimension_key ? 'dimension'
           ORDER BY window_end DESC, dimension_key->>'dimension', dimension_key->>'bucket'
           LIMIT 200"""
    ).fetchall()
    shadow_rows = conn.execute(
        """SELECT * FROM calibration_results
           WHERE dimension_key->>'type' = 'shadow_evaluation'
           ORDER BY window_end DESC LIMIT 100"""
    ).fetchall()
    versions = conn.execute(
        """SELECT id, created_at, status, proposed_by, approved_by, approved_at
           FROM strategy_config_versions ORDER BY created_at DESC LIMIT 20"""
    ).fetchall()
    lifecycle_events = conn.execute(
        """SELECT created_at, event_type, payload FROM system_events
           WHERE component = 'promotion' ORDER BY created_at DESC LIMIT 20"""
    ).fetchall()

    return {
        "calibration_buckets": [
            {
                "dimension": r["dimension_key"].get("dimension"),
                "bucket": escape_tree(r["dimension_key"].get("bucket")),
                "qualified": r["dimension_key"].get("qualified"),
                "window_start": jsonify(r["window_start"]),
                "window_end": jsonify(r["window_end"]),
                "sample_size": r["sample_size"],
                "metrics": jsonify(r["metrics"]),
            }
            for r in buckets
        ],
        "shadow_evaluations": [
            {
                "config_version_id": r["dimension_key"].get("config_version_id"),
                "metrics": escape_tree(r["metrics"]),
            }
            for r in shadow_rows
        ],
        "config_versions": [jsonify(dict(r)) for r in versions],
        "promotion_events": [
            {
                "at": jsonify(r["created_at"]),
                "event_type": r["event_type"],
                "payload": escape_tree(r["payload"]),
            }
            for r in lifecycle_events
        ],
    }


PANELS = {
    "system": panel_system,
    "equity": panel_equity,
    "greeks": panel_greeks,
    "opportunities": panel_opportunities,
    "positions": panel_positions,
    "orders": panel_orders,
    "performance": panel_performance,
}
