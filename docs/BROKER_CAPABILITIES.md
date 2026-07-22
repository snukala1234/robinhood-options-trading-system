# Broker Capabilities — Discovery Semantics and Setup

## Principle

The system never assumes the connected Robinhood account supports any order type,
strategy, or tool. The default is `MOST_RESTRICTIVE` (nothing supported); runtime
discovery (`src/execution/capabilities.py`) can only *add* capabilities based on the
MCP tools the connected account actually exposes. Strategies whose requirements are
not covered by the discovered capability set disappear from the candidate universe
(`executable_strategies`), and the adapters refuse — never emulate — anything
unsupported. In particular: **a spread is executed as one broker-native multi-leg
order or not at all**; independent-leg emulation does not exist in the codebase.

## Discovery semantics

- `discover_from_tools(tool_listing)` maps tool names to capability flags through
  `TOOL_CAPABILITY_MAP`. Unrecognized tools grant nothing.
- Multi-leg support additionally requires the multi-leg tool's schema to declare a
  `legs` array; debit/credit spread support requires those words in the schema.
  A bare tool name is not enough to trust atomic execution.
- Every discovery run is persisted to `broker_capability_snapshots` (exactly one
  `is_current` row) with the derived `executable_strategies` list, keyed by a
  SHA-256 hash of the account id — raw account identifiers are never stored.
- No limit-order support ⇒ no executable strategies at all (`ALLOW_MARKET_ORDERS`
  is permanently False).

## Current status (honest)

**No live Robinhood MCP transport is connected in this build.** The adapter
(`src/execution/robinhood_mcp.py`) is fully typed and contract-tested against a
mock transport; without a transport it fails with `BrokerUnavailable` — it never
fabricates a connection or a successful order. Live submission additionally raises
`LiveOrdersDisabled` while `ALLOW_LIVE_ORDERS=False` / `ORDER_MODE="research_only"`
(the entire build), before any transport call is made.

## Human setup steps (to be done manually, later, after Section 24 review)

1. Create/confirm a **dedicated Robinhood account** for agentic trading with options
   permissions appropriate to defined-risk strategies, and set it to
   **manual approval** for every order on the Robinhood side.
2. Obtain access to the Robinhood **Trading MCP** for that account and note the
   endpoint and authentication method.
3. Put credentials in `.env` only (never source control), e.g.
   `ROBINHOOD_MCP_URL=...`, `ROBINHOOD_ACCOUNT_ID=...`.
4. Implement/configure the `MCPTransport` binding (Phase H startup wiring) using
   those env vars.
5. **First-connect checklist:**
   - Run capability discovery and read the raw tool listing.
   - Review `TOOL_CAPABILITY_MAP` against the *actual* tool names and schemas —
     the map ships with plausible names and MUST be corrected to reality; any
     mismatch means capabilities stay off (fail-closed), not on.
   - Inspect the recorded `broker_capability_snapshots` row and confirm
     `executable_strategies` matches what the account can genuinely do.
   - Verify preview/cancel semantics with read-only and preview calls first.
6. Live orders remain impossible until the Section 24 checklist is complete and a
   human deliberately changes `ALLOW_LIVE_ORDERS`/`ORDER_MODE` — no code in this
   repository does it.
