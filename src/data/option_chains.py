"""Option-chain normalization (spec 5.2/5.3): provenance, freshness, fail-closed.

Raw provider payloads become :class:`ContractQuote` objects or raise — a quote with a
missing bid, a crossed market, a float where money belongs, or no timestamp never
enters the system. Every quote carries ``source``, ``observed_at``, ``received_at``
and can be checked for freshness against the hard ``MAX_QUOTE_AGE_SECONDS`` limit.

Broker-supplied Greeks must arrive under the normalized keys
``delta / gamma / theta_daily / vega`` (the Phase D adapter owns mapping each
broker's convention onto these); a partial set is rejected rather than padded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from src.analytics.greeks import GreekSet, GreekSource
from src.config.risk_policy import MAX_QUOTE_AGE_SECONDS
from src.domain.instruments import OptionContract, OptionType
from src.domain.values import DomainValidationError, require_utc


class StaleQuoteError(DomainValidationError):
    """The quote is older than the hard freshness limit and must not be used."""


def _dec(name: str, value: Any) -> Decimal:
    """Parse a numeric field into Decimal. Floats are rejected — they already drifted."""
    if isinstance(value, bool) or value is None:
        raise DomainValidationError(f"{name} is missing or invalid: {value!r}")
    if isinstance(value, float):
        raise DomainValidationError(
            f"{name} arrived as float ({value!r}); providers must send strings"
        )
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | str):
        try:
            return Decimal(str(value).strip())
        except InvalidOperation as exc:
            raise DomainValidationError(f"{name} is not a number: {value!r}") from exc
    raise DomainValidationError(f"{name} has unsupported type {type(value).__name__}")


def _int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainValidationError(f"{name} must be an int, got {value!r}")
    if value < 0:
        raise DomainValidationError(f"{name} must be >= 0, got {value}")
    return value


@dataclass(frozen=True)
class ContractQuote:
    """A normalized, validated point-in-time quote for one option contract."""

    contract: OptionContract
    bid: Decimal
    ask: Decimal
    midpoint: Decimal
    volume: int
    open_interest: int
    observed_at: datetime
    received_at: datetime
    source: str
    last: Decimal | None = None
    implied_volatility: Decimal | None = None
    greeks: GreekSet | None = None

    def age_seconds(self, now: datetime) -> float:
        now = require_utc("now", now)
        return (now - self.observed_at).total_seconds()

    def require_fresh(self, now: datetime) -> None:
        """Raise :class:`StaleQuoteError` when older than MAX_QUOTE_AGE_SECONDS."""
        age = self.age_seconds(now)
        if age < 0:
            raise StaleQuoteError(
                f"{self.contract.occ_symbol()}: observed_at is in the future "
                f"(clock skew, age={age:.1f}s)"
            )
        if age > MAX_QUOTE_AGE_SECONDS:
            raise StaleQuoteError(
                f"{self.contract.occ_symbol()}: quote is {age:.1f}s old "
                f"(limit {MAX_QUOTE_AGE_SECONDS}s)"
            )


def normalize_contract(raw: dict[str, Any], *, source: str, received_at: datetime) -> ContractQuote:
    """Validate one raw contract record. Raises on anything missing or malformed."""
    if not isinstance(source, str) or not source:
        raise DomainValidationError("source must be a non-empty string")
    received_at = require_utc("received_at", received_at)

    for key in (
        "underlying",
        "expiration",
        "strike",
        "option_type",
        "bid",
        "ask",
        "volume",
        "open_interest",
        "observed_at",
    ):
        if key not in raw:
            raise DomainValidationError(f"raw contract record missing {key!r}")

    expiration_raw = raw["expiration"]
    if isinstance(expiration_raw, date) and not isinstance(expiration_raw, datetime):
        expiration = expiration_raw
    elif isinstance(expiration_raw, str):
        try:
            expiration = date.fromisoformat(expiration_raw)
        except ValueError as exc:
            raise DomainValidationError(
                f"expiration is not ISO YYYY-MM-DD: {expiration_raw!r}"
            ) from exc
    else:
        raise DomainValidationError(f"expiration has bad type: {expiration_raw!r}")

    option_type_raw = raw["option_type"]
    try:
        option_type = OptionType(str(option_type_raw).lower())
    except ValueError as exc:
        raise DomainValidationError(f"option_type invalid: {option_type_raw!r}") from exc

    contract = OptionContract(
        underlying=str(raw["underlying"]),
        expiration=expiration,
        strike=_dec("strike", raw["strike"]),
        option_type=option_type,
    )

    observed_raw = raw["observed_at"]
    if isinstance(observed_raw, str):
        try:
            observed_at = require_utc("observed_at", datetime.fromisoformat(observed_raw))
        except ValueError as exc:
            raise DomainValidationError(f"observed_at is not ISO-8601: {observed_raw!r}") from exc
    else:
        observed_at = require_utc("observed_at", observed_raw)

    bid = _dec("bid", raw["bid"])
    ask = _dec("ask", raw["ask"])
    if bid < 0:
        raise DomainValidationError(f"bid must be >= 0, got {bid}")
    if ask <= 0:
        raise DomainValidationError(f"ask must be > 0, got {ask}")
    if bid > ask:
        raise DomainValidationError(f"crossed market: bid {bid} > ask {ask}")
    midpoint = (bid + ask) / 2

    iv: Decimal | None = None
    if raw.get("implied_volatility") is not None:
        iv = _dec("implied_volatility", raw["implied_volatility"])
        if not (Decimal("0") < iv < Decimal("10")):
            raise DomainValidationError(f"implied_volatility implausible: {iv}")

    greeks: GreekSet | None = None
    if raw.get("greeks") is not None:
        g = raw["greeks"]
        if not isinstance(g, dict):
            raise DomainValidationError("greeks must be a mapping")
        missing = {"delta", "gamma", "theta_daily", "vega"} - set(g)
        if missing:
            raise DomainValidationError(
                f"broker greeks incomplete, missing {sorted(missing)}; "
                "a partial set is rejected, never padded"
            )
        greeks = GreekSet(
            delta=_dec("greeks.delta", g["delta"]),
            gamma=_dec("greeks.gamma", g["gamma"]),
            theta_daily=_dec("greeks.theta_daily", g["theta_daily"]),
            vega=_dec("greeks.vega", g["vega"]),
            rho=_dec("greeks.rho", g["rho"]) if g.get("rho") is not None else None,
            source=GreekSource.BROKER,
        )

    last: Decimal | None = None
    if raw.get("last") is not None:
        last = _dec("last", raw["last"])
        if last < 0:
            raise DomainValidationError(f"last must be >= 0, got {last}")

    return ContractQuote(
        contract=contract,
        bid=bid,
        ask=ask,
        midpoint=midpoint,
        volume=_int("volume", raw["volume"]),
        open_interest=_int("open_interest", raw["open_interest"]),
        observed_at=observed_at,
        received_at=received_at,
        source=source,
        last=last,
        implied_volatility=iv,
        greeks=greeks,
    )


def normalize_chain(
    raw_contracts: list[dict[str, Any]], *, source: str, received_at: datetime
) -> tuple[ContractQuote, ...]:
    """Normalize a whole chain; duplicates are an error, and order is canonical."""
    quotes = [
        normalize_contract(raw, source=source, received_at=received_at) for raw in raw_contracts
    ]
    seen: set[str] = set()
    for q in quotes:
        occ = q.contract.occ_symbol()
        if occ in seen:
            raise DomainValidationError(f"duplicate contract in chain: {occ}")
        seen.add(occ)
    quotes.sort(
        key=lambda q: (
            q.contract.expiration,
            q.contract.strike,
            q.contract.option_type.value,
        )
    )
    return tuple(quotes)
