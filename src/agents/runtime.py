"""Agent runtime (spec Phase E / Section 16): model routing, failover, strict
validation with exactly one repair retry, and complete decision logging.

Rules implemented here:

- Model IDs resolve only through :mod:`src.config.models` aliases; nothing here or
  in any agent module names a model.
- Offline mode (``TRADING_LLM=offline``, the default) validates the agent's
  deterministic offline payload through the same strict schema and logs the same
  decision row — hermetic, reproducible, no network.
- Live mode walks the failover chain on *sustained* unavailability; transient
  errors retry the same model. A decision made on a non-primary model is tagged
  ``decided_under_failover=True``; combined with
  ``ALLOW_NEW_ENTRY_DURING_FAILOVER=False`` it can never open a new position
  (:func:`failover_blocks_new_entry`).
- Invalid output gets exactly ONE schema-repair retry on the same model, then the
  call fails closed with :class:`InvalidAgentOutput`. Coerced or partial output
  never proceeds.
- Every call — success, repair, or failure — is logged to ``agent_decisions``;
  total unavailability additionally writes a critical ``system_events`` row, which
  :func:`agent_unavailability_blocks_entries` turns into an entry halt.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, TypeVar

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ValidationError

from src.config.models import MAX_MODEL_UNAVAILABLE_RETRIES, model_chain
from src.config.risk_policy import ALLOW_NEW_ENTRY_DURING_FAILOVER
from src.persistence.db import Connection

TModel = TypeVar("TModel", bound=BaseModel)

#: Agents whose unavailability must stop new entries (spec Section 16). Position
#: management is deliberately absent: exits are protected by pure code, not LLMs.
REQUIRED_ENTRY_AGENTS = frozenset(
    {
        "market_regime",
        "catalyst_research",
        "technical_analyst",
        "options_specialist",
        "strategy_selector",
        "portfolio_manager",
        "risk_officer",
    }
)


class TransientModelError(RuntimeError):
    """Rate limit / timeout / 5xx: retry the SAME model."""


class ModelUnavailableError(RuntimeError):
    """Sustained failure (unknown model, revoked access): fail over."""


class AllModelsUnavailableError(RuntimeError):
    """Every model in the failover chain was unreachable."""


class InvalidAgentOutput(RuntimeError):
    """Output failed schema validation after the single repair retry. Fail closed."""


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    input_tokens: int | None
    output_tokens: int | None


class ModelProvider(Protocol):
    kind: str

    def generate(self, model: str, system: str, user: str) -> ProviderResponse: ...


class AnthropicProvider:
    """Real Anthropic client; maps SDK errors onto the transient/sustained taxonomy."""

    kind = "anthropic"

    def __init__(self, api_key: str, max_tokens: int = 1024) -> None:
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self._max_tokens = max_tokens

    def generate(self, model: str, system: str, user: str) -> ProviderResponse:
        a = self._anthropic
        try:
            response = self._client.messages.create(
                model=model,
                max_tokens=self._max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except (a.RateLimitError, a.APITimeoutError, a.APIConnectionError) as exc:
            raise TransientModelError(str(exc)) from exc
        except a.InternalServerError as exc:
            raise TransientModelError(str(exc)) from exc
        except a.APIStatusError as exc:
            raise ModelUnavailableError(str(exc)) from exc
        parts: list[str] = []
        for block in response.content:
            block_text = getattr(block, "text", None)
            if isinstance(block_text, str):
                parts.append(block_text)
        text = "".join(parts)
        usage = getattr(response, "usage", None)
        return ProviderResponse(
            text=text,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )


def resolve_mode() -> str:
    """``offline`` (default, hermetic) | ``live`` | ``auto`` (live iff key set)."""
    mode = os.environ.get("TRADING_LLM", "offline").strip().lower()
    if mode not in {"offline", "live", "auto"}:
        raise RuntimeError(f"TRADING_LLM must be offline|live|auto, got {mode!r}")
    if mode == "auto":
        return "live" if os.environ.get("ANTHROPIC_API_KEY") else "offline"
    return mode


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of model text (fenced or bare)."""
    match = _FENCE_RE.search(text)
    candidate = match.group(1) if match else None
    if candidate is None:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        candidate = text[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


@dataclass(frozen=True)
class AgentCallResult[TOut: BaseModel]:
    """A validated agent decision plus its full provenance."""

    output: TOut
    agent_key: str
    model_id: str
    provider: str
    prompt_version: str
    decided_under_failover: bool
    validation_status: str  # "ok" | "repaired"
    latency_ms: int
    correlation_id: uuid.UUID
    decision_id: uuid.UUID


def failover_blocks_new_entry(result: AgentCallResult[Any]) -> bool:
    """ALLOW_NEW_ENTRY_DURING_FAILOVER=False: fallback-model decisions cannot
    open new positions. (They may still inform risk-reducing action.)"""
    return result.decided_under_failover and not ALLOW_NEW_ENTRY_DURING_FAILOVER


def agent_unavailability_blocks_entries(
    conn: Connection, *, now: datetime, within_seconds: int = 900
) -> tuple[bool, tuple[str, ...]]:
    """New entries stop while any required agent was recently unavailable."""
    cutoff = now - timedelta(seconds=within_seconds)
    rows = conn.execute(
        "SELECT payload FROM system_events WHERE event_type = 'agent_unavailable' "
        "AND created_at >= %s",
        (cutoff,),
    ).fetchall()
    affected = sorted(
        {
            str(r["payload"].get("agent_key"))
            for r in rows
            if r["payload"].get("agent_key") in REQUIRED_ENTRY_AGENTS
        }
    )
    return (len(affected) > 0, tuple(affected))


@dataclass(frozen=True)
class AgentRuntime:
    """Shared call path for all nine agents."""

    conn: Connection
    provider: ModelProvider | None = None  # injected for live/tests; None = by mode

    def call(
        self,
        *,
        agent_key: str,
        prompt_version: str,
        system_prompt: str,
        user_prompt: str,
        output_schema: type[TModel],
        offline_payload: dict[str, Any],
        input_snapshot_ids: Sequence[uuid.UUID],
        correlation_id: uuid.UUID,
    ) -> AgentCallResult[TModel]:
        started = time.monotonic()
        chain = model_chain(agent_key)  # KeyError on unknown agent: fail loudly
        try:
            if self.provider is not None:
                result = self._call_live(
                    self.provider, chain, system_prompt, user_prompt, output_schema
                )
            elif resolve_mode() == "live":
                api_key = os.environ.get("ANTHROPIC_API_KEY", "")
                if not api_key:
                    raise AllModelsUnavailableError("live mode without ANTHROPIC_API_KEY")
                result = self._call_live(
                    AnthropicProvider(api_key), chain, system_prompt, user_prompt, output_schema
                )
            else:
                result = self._call_offline(chain[0], offline_payload, output_schema)
        except (InvalidAgentOutput, AllModelsUnavailableError) as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            decision_id = self._record(
                agent_key=agent_key,
                correlation_id=correlation_id,
                model_id=chain[0],
                prompt_version=prompt_version,
                input_snapshot_ids=input_snapshot_ids,
                output={"error": str(exc)},
                validation_result={"status": "failed", "error": str(exc)},
                latency_ms=latency_ms,
                token_usage=None,
            )
            if isinstance(exc, AllModelsUnavailableError):
                self._record_unavailable(agent_key, correlation_id, str(exc))
            raise
        output, model_id, provider_kind, failover, status, tokens = result
        latency_ms = int((time.monotonic() - started) * 1000)
        decision_id = self._record(
            agent_key=agent_key,
            correlation_id=correlation_id,
            model_id=model_id,
            prompt_version=prompt_version,
            input_snapshot_ids=input_snapshot_ids,
            output=output.model_dump(mode="json"),
            validation_result={"status": status, "decided_under_failover": failover},
            latency_ms=latency_ms,
            token_usage=tokens,
        )
        return AgentCallResult(
            output=output,
            agent_key=agent_key,
            model_id=model_id,
            provider=provider_kind,
            prompt_version=prompt_version,
            decided_under_failover=failover,
            validation_status=status,
            latency_ms=latency_ms,
            correlation_id=correlation_id,
            decision_id=decision_id,
        )

    # -- paths --------------------------------------------------------------

    def _call_offline(
        self, model_id: str, offline_payload: dict[str, Any], schema: type[TModel]
    ) -> tuple[TModel, str, str, bool, str, dict[str, int] | None]:
        try:
            output = schema.model_validate(offline_payload)
        except ValidationError as exc:
            # An invalid offline payload is a code bug: fail closed, no repair.
            raise InvalidAgentOutput(f"offline payload failed validation: {exc}") from exc
        return output, model_id, "offline", False, "ok", None

    def _call_live(
        self,
        provider: ModelProvider,
        chain: list[str],
        system_prompt: str,
        user_prompt: str,
        schema: type[TModel],
    ) -> tuple[TModel, str, str, bool, str, dict[str, int] | None]:
        last_error: Exception | None = None
        for model_index, model_id in enumerate(chain):
            response = self._generate_with_retries(provider, model_id, system_prompt, user_prompt)
            if response is None:
                last_error = ModelUnavailableError(f"{model_id} unavailable")
                continue
            output, status, tokens = self._validate_with_one_repair(
                provider, model_id, system_prompt, user_prompt, response, schema
            )
            return output, model_id, provider.kind, model_index > 0, status, tokens
        raise AllModelsUnavailableError(str(last_error or "no models configured"))

    def _generate_with_retries(
        self, provider: ModelProvider, model_id: str, system: str, user: str
    ) -> ProviderResponse | None:
        for _ in range(MAX_MODEL_UNAVAILABLE_RETRIES):
            try:
                return provider.generate(model_id, system, user)
            except TransientModelError:
                continue
            except ModelUnavailableError:
                return None
        return None  # transient errors exhausted: treat as unavailable

    def _validate_with_one_repair(
        self,
        provider: ModelProvider,
        model_id: str,
        system: str,
        user: str,
        response: ProviderResponse,
        schema: type[TModel],
    ) -> tuple[TModel, str, dict[str, int] | None]:
        tokens = _tokens(response)
        output, error = _try_validate(response.text, schema)
        if output is not None:
            return output, "ok", tokens
        repair_user = (
            f"{user}\n\nYour previous reply was rejected by schema validation:\n"
            f"{error}\n\nReturn ONLY a corrected JSON object matching the schema. "
            "No prose, no code fences, no extra fields."
        )
        try:
            repair_response = provider.generate(model_id, system, repair_user)
        except (TransientModelError, ModelUnavailableError) as exc:
            raise InvalidAgentOutput(
                f"invalid output and repair attempt failed to run: {exc}"
            ) from exc
        tokens = _merge_tokens(tokens, _tokens(repair_response))
        output, error = _try_validate(repair_response.text, schema)
        if output is not None:
            return output, "repaired", tokens
        raise InvalidAgentOutput(f"output invalid after one repair retry: {error}")

    # -- persistence --------------------------------------------------------

    def _record(
        self,
        *,
        agent_key: str,
        correlation_id: uuid.UUID,
        model_id: str,
        prompt_version: str,
        input_snapshot_ids: Sequence[uuid.UUID],
        output: dict[str, Any],
        validation_result: dict[str, Any],
        latency_ms: int,
        token_usage: dict[str, int] | None,
    ) -> uuid.UUID:
        decision_id = uuid.uuid4()
        self.conn.execute(
            """INSERT INTO agent_decisions
               (id, correlation_id, agent_name, created_at, model_id, prompt_version,
                input_snapshot_ids, output, validation_result, latency_ms, token_usage)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                decision_id,
                correlation_id,
                agent_key,
                datetime.now(UTC),
                model_id,
                prompt_version,
                Jsonb([str(s) for s in input_snapshot_ids]),
                Jsonb(output),
                Jsonb(validation_result),
                latency_ms,
                Jsonb(token_usage) if token_usage is not None else None,
            ),
        )
        return decision_id

    def _record_unavailable(self, agent_key: str, correlation_id: uuid.UUID, detail: str) -> None:
        self.conn.execute(
            """INSERT INTO system_events
               (id, created_at, severity, component, event_type, correlation_id, payload)
               VALUES (%s, %s, 'critical', 'agents', 'agent_unavailable', %s, %s)""",
            (
                uuid.uuid4(),
                datetime.now(UTC),
                correlation_id,
                Jsonb({"agent_key": agent_key, "detail": detail}),
            ),
        )


def _try_validate[T: BaseModel](text: str, schema: type[T]) -> tuple[T | None, str]:
    payload = extract_json(text)
    if payload is None:
        return None, "no JSON object found in model output"
    try:
        return schema.model_validate(payload), ""
    except ValidationError as exc:
        return None, str(exc)


def _tokens(response: ProviderResponse) -> dict[str, int] | None:
    if response.input_tokens is None and response.output_tokens is None:
        return None
    return {
        "input": response.input_tokens or 0,
        "output": response.output_tokens or 0,
    }


def _merge_tokens(a: dict[str, int] | None, b: dict[str, int] | None) -> dict[str, int] | None:
    if a is None:
        return b
    if b is None:
        return a
    return {"input": a["input"] + b["input"], "output": a["output"] + b["output"]}
