"""Money precision: awkward decimals round-trip through NUMERIC with zero drift.

psycopg3 adapts NUMERIC to/from ``decimal.Decimal`` natively; these tests prove the
full path (Python Decimal -> Postgres NUMERIC -> Python Decimal) is exact for the
values that expose float contamination: 0.1, 1.005, a large max loss, and a
sub-penny option price.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import psycopg

NOW = datetime(2026, 7, 21, 15, 30, tzinfo=UTC)

AWKWARD = {
    "classic_binary_trap": Decimal("0.1"),
    "midpoint_rounding_trap": Decimal("1.005"),
    "large_max_loss": Decimal("12345678901234.5678"),
    "sub_penny_option_price": Decimal("0.0001"),
}


def test_awkward_decimals_roundtrip_exactly(
    conn: psycopg.Connection[Any],
) -> None:
    conn.execute(
        """INSERT INTO option_contract_snapshots
           (id, underlying, option_symbol, expiration, strike, option_type,
            observed_at, bid, ask, midpoint, theta)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            uuid.uuid4(),
            "SPY",
            "SPY260918C00600000",
            date(2026, 9, 18),
            AWKWARD["midpoint_rounding_trap"],
            "call",
            NOW,
            AWKWARD["classic_binary_trap"],
            AWKWARD["sub_penny_option_price"],
            Decimal("0.05005"),
            Decimal("-6.20"),
        ),
    )
    row = conn.execute(
        "SELECT strike, bid, ask, midpoint, theta FROM option_contract_snapshots"
    ).fetchone()
    assert row is not None
    assert type(row["strike"]) is Decimal and row["strike"] == Decimal("1.005")
    assert type(row["bid"]) is Decimal and row["bid"] == Decimal("0.1")
    assert type(row["ask"]) is Decimal and row["ask"] == Decimal("0.0001")
    assert row["midpoint"] == Decimal("0.05005")
    assert row["theta"] == Decimal("-6.20")
    # The value float64 cannot represent: exactness here proves no float touched it.
    assert str(row["bid"]) == "0.1"
    assert row["bid"] + Decimal("0.2") == Decimal("0.3")


def test_large_max_loss_roundtrips_exactly(
    conn: psycopg.Connection[Any],
) -> None:
    conn.execute(
        """INSERT INTO positions
           (id, proposal_id, underlying, strategy, expiration, legs, opened_at,
            entry_net_price, quantity, max_loss, status, exit_plan)
           VALUES (%s, NULL, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s::jsonb)""",
        (
            uuid.uuid4(),
            "SPY",
            "bull_call_debit_spread",
            date(2026, 8, 7),
            "[]",
            NOW,
            Decimal("1.85"),
            1,
            AWKWARD["large_max_loss"],
            "open",
            "{}",
        ),
    )
    row = conn.execute("SELECT max_loss, entry_net_price FROM positions").fetchone()
    assert row is not None
    assert type(row["max_loss"]) is Decimal
    assert row["max_loss"] == Decimal("12345678901234.5678")
    # float64 would have mangled the tail digits; prove it did not happen.
    assert str(row["max_loss"]) == "12345678901234.5678"
    assert row["entry_net_price"] == Decimal("1.85")


def test_numeric_arithmetic_is_exact_in_database(
    conn: psycopg.Connection[Any],
) -> None:
    """NUMERIC arithmetic inside Postgres is exact: 0.1 + 0.2 equals 0.3, no epsilon.

    (The domain boundary already rejects floats via require_money; this proves the
    database side of the pipeline is just as drift-free.)
    """
    conn.execute(
        """INSERT INTO portfolio_snapshots
           (id, observed_at, total_equity, settled_cash, unsettled_cash, open_risk,
            is_paper)
           VALUES (%s, %s, %s, %s, %s, %s, TRUE)""",
        (
            uuid.uuid4(),
            NOW,
            Decimal("0.1"),
            Decimal("0.2"),
            Decimal("0.3"),
            Decimal("0"),
        ),
    )
    row = conn.execute(
        "SELECT total_equity + settled_cash AS s, unsettled_cash FROM portfolio_snapshots"
    ).fetchone()
    assert row is not None
    # Exact NUMERIC arithmetic in the database: 0.1 + 0.2 == 0.3, no epsilon needed.
    assert row["s"] == row["unsettled_cash"] == Decimal("0.3")
