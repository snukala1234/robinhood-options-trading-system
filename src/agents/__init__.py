"""V2 reasoning committee (spec Section 6, Phase E).

Nine isolated interpretive agents. Structural rules:

- Agents consume only validated deterministic feature packets (Phase C outputs).
  They never compute trusted risk/Greek/max-loss numbers — those arrive in the
  packet — and their outputs are opinions for the deterministic gate, never
  commands.
- Agents have **no tools**. The runtime sends text and receives text; there is no
  tool-execution path anywhere in this package, so untrusted content inside a
  prompt has nothing to invoke (test-enforced: nothing here imports
  ``src.execution``).
- Every call is schema-validated (strict Pydantic, ``extra="forbid"``); an invalid
  output gets exactly one repair retry, then the call fails closed. No partial or
  coerced output ever proceeds.
- Every call — including failures — is logged to ``agent_decisions`` with model
  ID, prompt version, input snapshot IDs, validation result, latency, token usage,
  and correlation ID.
"""
