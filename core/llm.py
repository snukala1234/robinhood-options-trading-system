"""The model-call interface used by every LLM-backed agent (Sections 3.7, 3.8).

Design goals:
- Model selection is resolved ONLY through :mod:`config.models`; no agent hardcodes a model.
- Provider is pluggable. When ``ANTHROPIC_API_KEY`` is set (and mode != offline) the real
  Anthropic client is used; otherwise a deterministic *offline* path returns the caller's
  supplied ``offline_result`` so paper runs are hermetic and reproducible.
- Automatic failover (Section 3.8): a *sustained* model-unavailability walks the
  ``FAILOVER_CHAIN``; the failover is logged and the decision is tagged
  ``decided_under_failover=True`` so Agent 8 can evaluate failover-model decisions separately.
- Transient errors (rate limit / timeout / 5xx) are retried on the SAME model up to
  ``MAX_MODEL_UNAVAILABLE_RETRIES`` before that model is treated as unavailable.

Mode is controlled by ``TRADING_LLM``: "offline" (default hermetic), "live", or "auto"
(live iff an API key is present).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from config.models import MAX_MODEL_UNAVAILABLE_RETRIES, model_chain, model_for
from core.event_bus import bus
from core.logging_setup import get_logger, log_decision

if TYPE_CHECKING:
    from core.db import Database

_log = get_logger("llm")


# --- error taxonomy ---------------------------------------------------------


class TransientModelError(Exception):
    """A transient failure (rate limit / timeout / 5xx). Retry the SAME model."""


class ModelUnavailableError(Exception):
    """A sustained failure (model not found / access removed). Trigger failover."""


class AllModelsUnavailableError(Exception):
    """Every model in the failover chain was unreachable."""


# --- result -----------------------------------------------------------------


@dataclass
class LLMResult:
    data: dict[str, Any]
    text: str
    active_model: str
    provider: str  # "offline" | "anthropic"
    decided_under_failover: bool
    attempts: int


# --- provider protocol + implementations ------------------------------------


class LLMProvider(Protocol):
    kind: str

    def generate(self, model: str, system: str, user: str) -> str:
        """Return raw model text (expected to contain a JSON object)."""
        ...


class AnthropicProvider:
    """Real Anthropic client. Maps SDK errors onto the transient/sustained taxonomy."""

    kind = "anthropic"

    def __init__(self, api_key: str, max_tokens: int = 1024) -> None:
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self._max_tokens = max_tokens

    def generate(self, model: str, system: str, user: str) -> str:
        a = self._anthropic
        try:
            message = self._client.messages.create(
                model=model,
                max_tokens=self._max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except a.NotFoundError as exc:  # model id not available / access removed
            raise ModelUnavailableError(f"{model}: not found") from exc
        except a.PermissionDeniedError as exc:  # export-control style access removal
            raise ModelUnavailableError(f"{model}: permission denied") from exc
        except (a.RateLimitError, a.APITimeoutError, a.InternalServerError) as exc:
            raise TransientModelError(f"{model}: {type(exc).__name__}") from exc
        except a.APIConnectionError as exc:
            raise TransientModelError(f"{model}: connection error") from exc

        parts = [
            getattr(block, "text", "")
            for block in message.content
            if getattr(block, "type", None) == "text"
        ]
        return "".join(parts)


class OfflineProvider:
    """Marker provider for hermetic runs. Content comes from the caller's offline_result."""

    kind = "offline"

    def generate(self, model: str, system: str, user: str) -> str:  # pragma: no cover - unused
        raise RuntimeError("OfflineProvider.generate must not be called; use offline_result")


# --- mode / factory ---------------------------------------------------------


def llm_mode() -> str:
    return os.environ.get("TRADING_LLM", "offline").lower()


def _default_provider() -> LLMProvider:
    mode = llm_mode()
    key = os.environ.get("ANTHROPIC_API_KEY")
    if mode == "offline":
        return OfflineProvider()
    if mode == "live" or (mode == "auto" and key):
        if not key:
            raise RuntimeError("TRADING_LLM=live but ANTHROPIC_API_KEY is not set")
        return AnthropicProvider(api_key=key)
    return OfflineProvider()


# --- JSON extraction --------------------------------------------------------


def extract_json(text: str) -> dict[str, Any]:
    """Best-effort parse of the first JSON object in a model response."""
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
        obj = json.loads(candidate)
        if isinstance(obj, dict):
            return obj
    raise ValueError("no JSON object found in model response")


# --- client -----------------------------------------------------------------


class ModelClient:
    """Routes agent calls to a model with automatic failover and audit logging."""

    def __init__(self, provider: LLMProvider | None = None, db: Database | None = None) -> None:
        self.provider = provider if provider is not None else _default_provider()
        self.db = db

    def complete_json(
        self,
        agent_key: str,
        system: str,
        user: str,
        offline_result: dict[str, Any],
        *,
        agent_name: str | None = None,
    ) -> LLMResult:
        """Return structured JSON for ``agent_key``.

        ``offline_result`` is the deterministic stand-in used on the offline path (and is
        the schema contract the online path is expected to satisfy). ``agent_name`` labels
        audit events (defaults to ``agent_key``).
        """
        name = agent_name or agent_key

        if self.provider.kind == "offline":
            model = model_for(agent_key)
            log_decision(_log, "llm_offline", agent=name, model=model)
            return LLMResult(
                data=offline_result,
                text=json.dumps(offline_result),
                active_model=model,
                provider="offline",
                decided_under_failover=False,
                attempts=0,
            )

        return self._complete_live(agent_key, system, user, name)

    def _complete_live(
        self, agent_key: str, system: str, user: str, name: str
    ) -> LLMResult:
        chain = model_chain(agent_key)
        attempts = 0
        last_error: Exception | None = None

        for idx, model in enumerate(chain):
            transient_tries = 0
            while True:
                attempts += 1
                try:
                    text = self.provider.generate(model, system, user)
                    data = extract_json(text)
                    # Each failover hop is already recorded in _hop_to_next; the success
                    # under a fallback model is just tagged, not re-logged (avoids double count).
                    decided_under_failover = idx > 0
                    return LLMResult(
                        data=data,
                        text=text,
                        active_model=model,
                        provider=self.provider.kind,
                        decided_under_failover=decided_under_failover,
                        attempts=attempts,
                    )
                except TransientModelError as exc:
                    transient_tries += 1
                    last_error = exc
                    if transient_tries >= MAX_MODEL_UNAVAILABLE_RETRIES:
                        # Escalate: treat sustained transient failure as unavailable.
                        self._hop_to_next(name, chain, idx, f"transient_exhausted:{exc}")
                        break
                    continue
                except ModelUnavailableError as exc:
                    last_error = exc
                    self._hop_to_next(name, chain, idx, f"unavailable:{exc}")
                    break

        raise AllModelsUnavailableError(
            f"all models unavailable for {name}: {last_error}"
        ) from last_error

    def _hop_to_next(self, name: str, chain: list[str], idx: int, reason: str) -> None:
        if idx + 1 < len(chain):
            self._record_failover(name, chain[idx], chain[idx + 1], reason)

    def _record_failover(self, name: str, from_model: str, to_model: str, reason: str) -> None:
        log_decision(
            _log, "model_failover", agent=name, from_model=from_model,
            to_model=to_model, reason=reason,
        )
        if self.db is not None:
            self.db.insert_failover_event(
                agent_name=name,
                requested_model=from_model,
                fell_back_to=to_model,
                reason=reason,
            )
        bus.publish(
            {
                "type": "failover",
                "agent_name": name,
                "from_model": from_model,
                "to_model": to_model,
                "reason": reason,
            }
        )


def get_client(db: Database | None = None) -> ModelClient:
    """Convenience factory using the env-selected provider."""
    return ModelClient(db=db)
