# V1 → V2 Traceability Matrix and Migration Plan

**Phase A deliverable** (Section 22 master build prompt, "Phase A - Discovery and migration plan").

- **V1:** 8-agent cash-account *equities* paper-trading system (this repository, commit `5ce3075` baseline).
- **V2 target:** Options-first portfolio management and execution system per
  `Robinhood_Options_Trading_System_V2_Architecture_and_Build_Spec.md`.
- **Safety invariants held throughout the migration:** `PAPER_TRADING=True`,
  `ALLOW_LIVE_ORDERS=False`, `ORDER_MODE="research_only"`. No live order is ever placed
  during the build. No risk limit, settlement rule, test, or acceptance criterion is weakened.

Disposition legend:

| Code | Meaning |
|---|---|
| **RETAIN** | Kept substantially as-is (possibly moved/renamed) |
| **ADAPT** | Core logic/pattern kept, reworked for options semantics |
| **REWRITE** | Concept survives, implementation replaced |
| **REMOVE** | No V2 role; archived with the V1 baseline, not carried forward |
| **NEW** | V2 component with no V1 ancestor |

---

## 1. Traceability matrix

### 1.1 Configuration layer

> **Namespace decision:** all V2 configuration lives under the V2 namespace, `src/config/`,
> for the duration of the build — V1's top-level `config/` package is never edited (its
> `models.py` would otherwise collide with the Section 21 layout while still being imported
> by every V1 agent). In Phase K, when V1 is archived to `legacy_v1/`, `src/config/` is
> lifted to top-level `config/` to match Section 21 exactly.

| V1 component | What it does | Disposition | V2 target | Notes |
|---|---|---|---|---|
| `config/guardrails.py` | Section-0 hard guardrails (equity stop %, position %, halts), `HARD_GUARDRAIL_NAMES` frozenset, code-not-prompt principle | **REWRITE** | `src/config/risk_policy.py` | The *pattern* (pure-code, frozenset of protected names, agents may only recommend) is retained exactly. Every value is replaced by the V2 Section 3 policy: DTE bands, max-loss %, Greek limits, liquidity floors, `PAPER_TRADING=True`, `ALLOW_LIVE_ORDERS=False`, `ORDER_MODE="research_only"`, kill-switch flags. |
| `config/strategy.py` (`StrategyParams`, clamp ranges) | Tunable params Agent 8 may adapt, pre-approved ranges, guardrail non-overlap | **ADAPT** | `src/config/tunables.py` (+ `strategy_config_versions` rows) | Range-clamped immutable dataclass + "tunables never overlap guardrails" invariant carried over; fields become options-native (score weights, exit thresholds, DTE review points). |
| `config/models.py` | Central model routing, failover chain, `model_chain()` | **RETAIN → ADAPT** | `src/config/models.py` | New module in the V2 namespace (V1's file is not edited — it collides by name with the Section 21 layout and is imported by every V1 agent). Keep failover chain + no-hardcoded-model rule (already test-enforced); the V2 sweep test scopes to `src/`. Re-key to the nine V2 agents; map aliases (`CLAUDE_REASONING_MODEL` / `CLAUDE_BALANCED_MODEL`) through environment config per Section 16. |
| `config/settings.py` | DB path, timezone, universe, phase table | **ADAPT** | `src/config/environments.py` | DB path/timezone/env-override pattern retained. Equity `DEFAULT_UNIVERSE` becomes an optionable-underlying universe config; the Section-5 equity "phase table" is superseded by V2 operating modes 0–4 (Section 20). |
| — | — | **NEW** | `src/config/strategy_registry.py` | Section 7.1 `STRATEGY_REGISTRY` with capability requirements per structure; intersected with broker capabilities at runtime. |

### 1.2 Core infrastructure

| V1 component | What it does | Disposition | V2 target | Notes |
|---|---|---|---|---|
| `core/db.py` (`Database` wrapper, SQLite schema) | 10-table equity schema: `trade_journal` (shares/entry_price), `signal_history`, `orders` mirror, calibration/shadow/config tables | **ADAPT (wrapper) / REWRITE (schema)** | `src/persistence/models.py`, `repositories.py`, `migrations/` | The thin typed-wrapper + `is_paper` tagging + config-version pattern is reusable. The schema is replaced by the Section 14 DDL (14 tables incl. `option_contract_snapshots`, `order_events` append-only, `agent_decisions`, `broker_capability_snapshots`). **Locked decision (2026-07-21): PostgreSQL only — prototype, paper, and live.** No SQLite anywhere in V2; the Section 14 DDL runs verbatim on Postgres (native UUID, TIMESTAMPTZ, JSONB, NUMERIC) through Alembic versioned, reversible migrations, and psycopg3 adapts NUMERIC↔`Decimal` natively. `strategy_config_versions`, `calibration`-style and `system_events`-style tables migrate conceptually intact. |
| `core/records.py` (dataclass DTOs) | `MarketSnapshot` (price/ATR), `ResearchSignal`, `AggregatedSignal`, `Position` (shares) | **REWRITE** | `src/domain/*` | Plain-dataclass DTO pattern retained; shapes replaced by options domain models (contracts, legs, strategies, proposals, orders, positions, portfolio snapshots) using `Decimal` for money. |
| `core/llm.py` (`ModelClient`, offline provider, failover, `decided_under_failover` tagging) | Provider-pluggable LLM interface; hermetic offline mode; transient-vs-sustained error taxonomy | **RETAIN → ADAPT** | `src/agents/` runtime (shared client) | One of the strongest V1 assets. Add: JSON-Schema/Pydantic output validation, one schema-repair retry then fail-closed, per-call logging of snapshot IDs / prompt version / latency / tokens / correlation ID into `agent_decisions` (Section 16 + Phase E). |
| `core/event_bus.py` | In-process bus + asyncio/WebSocket bridge, bounded history, never-raises publishing | **RETAIN** | `src/orchestration/event_bus.py` | Reusable as-is. V2 adds event types (order state changes, kill switches, session transitions). |
| `core/logging_setup.py`, `core/util.py` | Structured decision logging, UUID/ISO-time helpers | **RETAIN** | `src/observability/logging.py`, shared utils | Extend with correlation IDs, account-hash redaction, tamper-evident audit hashing (Section 17). |
| `core/stats.py` | z-scores, Sharpe, significance helpers | **RETAIN** | `src/analytics/` + calibration | Asset-neutral; feeds V2 calibration (Brier score etc. added). |
| `core/market_data.py` | yfinance OHLCV/ATR + deterministic offline fixtures, snapshot-provider seam, regime labels | **ADAPT** | `src/data/market_data.py` | Offline-fixture discipline, mode env-var (`auto/offline/online`), and the point-in-time provider seam are retained. Extended for freshness metadata (`observed_at`, `age_seconds`, quality flags per Section 5.2). Options chains are a NEW service — yfinance-based equity snapshots remain only as underlying data. |

### 1.3 Risk layer

| V1 component | What it does | Disposition | V2 target | Notes |
|---|---|---|---|---|
| `risk/sizing.py` — `settled_cash()`, `assert_purchase_is_covered()`, `Account`/`Sale` T+1 model | Settled-cash / good-faith-violation guard | **RETAIN → ADAPT** | `src/risk/settlement.py` | Direct ancestor of Section 11. Extended for: debit + fees reservation, credit-spread collateral, projected settlement dates, exits-never-blocked rule. |
| `risk/sizing.py` — `calculate_position_size()` (ATR/confidence/correlation share sizing) | Equity dollar sizing | **REWRITE** | `src/risk/sizing.py` | Replaced by Section 9 max-loss/risk-budget contract sizing (`Decimal`, per-trade / portfolio / cluster / settled-cash minimum). The min-of-budgets shape and "quantity 0 = no trade" survive conceptually. |
| `risk/sizing.py` — `check_stop_loss*` | % price stop, forced, non-overridable | **REWRITE** | `src/risk/circuit_breakers.py` + exit engine | "Forced exits are pure code, no LLM" principle retained verbatim; per-share stop math replaced by premium/underlying/time/vol/event exits + deterministic emergency exits (Section 10). |
| `risk/sizing.py` — `check_portfolio_halt()` | Daily-loss / drawdown halts | **ADAPT** | `src/risk/circuit_breakers.py` | Same mechanism; new thresholds (2%/2.5%/5%/10%) plus weekly window, kill switches, manual-resume rule. |
| `risk/sizing.py` — `sector_correlation()` static map | Coarse sector correlation proxy | **REWRITE** | `src/analytics/portfolio_exposure.py` | Superseded by cluster/correlation exposure aggregation over underlyings and sectors. |
| — | — | **NEW** | `src/risk/trade_gate.py` | Section 3.1 ten-step guardrail precedence; issues short-lived, proposal/account/quote-bound approval tokens (Phase F). No V1 equivalent — V1's `RiskManager.evaluate()` is its closest ancestor pattern. |

### 1.4 Agents

| V1 agent | Disposition | V2 successor(s) | Notes |
|---|---|---|---|
| Scanner (Agent 1) | **ADAPT** | Universe filter service + candidate generation | Prefilter→rank pattern kept; share-volume floors replaced by optionable-universe filters (chain liquidity, OI). LLM ranking moves behind deterministic filters. |
| Research: technical (Agent 2a) | **ADAPT** | Agent 3 — Technical Structure Analyst | Template-method base (`agents/research/base.py`) is the direct pattern ancestor for all nine V2 agents. Equity volume/ATR heuristics replaced by interpretation of the deterministic technical feature service. |
| Research: fundamental (Agent 2b) | **REMOVE** | — (catalyst research absorbs the useful part) | Equity valuation research has no direct V2 role; earnings timing lives in Agent 2 (Universe & Catalyst Researcher). |
| Research: sentiment (Agent 2c) | **REMOVE** | — (catalyst research) | Same. |
| Research: macro (Agent 2d) | **ADAPT** | Agent 1 — Market Regime Strategist | Regime-driven reasoning is the closest V1→V2 carryover among research agents. |
| Edge Aggregator (Agent 3) | **REWRITE** | Deterministic opportunity-scoring service (Section 5.8) + Agent 5/6 reasoning | "Numbers are code, LLM narrates" guardrail carried over; the ensemble itself becomes the deterministic `OpportunityScore`. |
| Portfolio Construction (Agent 4) | **REWRITE** | Agent 5 — Strategy Selection Specialist + Agent 6 — Portfolio Manager + deterministic sizing | Reject-with-reason and confidence-gate patterns retained; share proposals become full options-structure proposals (Section 8.1 schema). |
| Risk Manager (Agent 5) | **ADAPT + SPLIT** | Agent 7 — Independent Risk Officer (reasoning veto) + `trade_gate.py` (deterministic) | V1 already split "code decides, LLM annotates"; V2 makes both halves first-class: a reasoning Risk Officer with veto and a deterministic gate that no approval can override. |
| Execution (Agent 6): `Broker` Protocol, `PaperBroker`, inert `RobinhoodMCPBroker` stub | **ADAPT (interface) / REWRITE (impl)** | `src/execution/robinhood_mcp.py`, `paper_broker.py`, `order_state_machine.py`, `reconciliation.py`, `idempotency.py` | Broker-Protocol + paper/live isolation + "no code path confirms a live order" architecture is retained. V2 adds: typed MCP adapter behind interface, runtime capability discovery, append-only order-event state machine, idempotency keys, reconciliation, multi-leg atomicity rules. V1's flat status strings are superseded by the Section 12.2 state machine. |
| Exit Monitor (Agent 7) | **ADAPT** | Agent 8 — Position Management Analyst + deterministic exit engine | "Pure-code forced exits run before any LLM; model outage degrades to code-only" is retained exactly (test-proven in V1). Price-stop/TP math replaced by five-dimension exit plans (Section 10). |
| Auditor (Agent 8) + `agents/calibration.py` | **RETAIN → ADAPT** | Agent 9 — Performance & Calibration Auditor | Largest single reusable agent asset (~536 lines): bucket stats, z-gates, `MIN_SAMPLE_SIZE` gating, shadow configs, human-gated promotion, `_assert_no_guardrail_keys`. Re-dimensioned to options buckets (strategy/DTE/delta/gamma-theta/IV/regime…, Section 13.2) with MAE/MFE, slippage, Brier score. |
| — | **NEW** | Agent 2 — Universe & Catalyst Researcher | No V1 ancestor (earnings/catalyst timing vs expiration, IV-crush risk). |
| — | **NEW** | Agent 4 — Volatility & Options Structure Specialist | No V1 ancestor. |

### 1.5 Orchestration and entrypoints

| V1 component | Disposition | V2 target | Notes |
|---|---|---|---|
| `orchestrator.py` (`Orchestrator`, `PaperPortfolio`) | **ADAPT (portfolio) / REWRITE (loop)** | `src/orchestration/session_controller.py`, `workflows.py` | `PaperPortfolio`'s T+1 settled/unsettled mechanics feed the V2 paper broker + settlement service. The linear entry/monitor loop is replaced by the Section 15 session state machine (OFFLINE→…→HALTED) with startup validation and event-driven monitoring. The "no new capital under model failover" rule generalizes to `ALLOW_NEW_ENTRY_DURING_FAILOVER=False`. |
| `run_paper.py` | **ADAPT** | `scripts/launch_paper.py` | Hermetic-by-default env pattern retained; deterministic sine price path replaced by options fixtures (chains + fills). |
| `run_backtest.py`, `backtest/engine.py`, `backtest/data.py`, `backtest/fundamentals.py`, `backtest/variants.py`, `backtest/compare.py`, `backtest/report.py` | **ADAPT (harness patterns) / REWRITE (data+fills)** | `tests/fixtures` + Section 19.4 historical simulation | Retained patterns: point-in-time no-look-ahead store, walk-forward driving the unchanged production pipeline, A/B decision diffing, honest-caveats reporting. Equity OHLC bars/fill logic cannot price options; per Section 19.4, without point-in-time chains results must be labeled *limited* — V2 keeps the harness but gates its claims. |
| `backtest/cost.py`, `estimate_cost.py`, `compare_slice.py` | **RETAIN** | `scripts/` (cost planning) | `MeteringModelClient` cost estimator is asset-neutral and directly reusable for V2 LLM budgeting. |

### 1.6 Dashboard and tests

| V1 component | Disposition | V2 target | Notes |
|---|---|---|---|
| `dashboard/api.py` (read-only FastAPI + WS; no mutating routes; no execution import — both test-enforced) | **RETAIN (architecture) / REWRITE (panels)** | `src/api/read_only.py`, `websockets.py`, `src/dashboard/frontend/` | Read-only-by-construction pattern and its structural tests carry over verbatim. Panels rebuilt per Section 18 (Greeks, opportunity board, orders/reconciliation, calibration). |
| `tests/` (107 tests; conftest hermetic fixtures; guardrail-integrity, no-hardcoded-model, dashboard-read-only, execution-never-confirms tests) | **RETAIN (harness + invariant tests) / REWRITE (domain tests)** | `tests/unit`, `property`, `integration`, `chaos` | The invariant-style tests (guardrail drift check, model-string sweep, read-only assertion, no-confirm broker) are patterns V2 must re-implement against new modules. Domain tests are rewritten for options. V2 adds property-based (Hypothesis) and chaos suites — NEW. |
| `pyproject.toml` (ruff + strict mypy py3.13 + pytest) | **RETAIN → EXTEND** | `pyproject.toml` + lockfile | Add dependency lockfile, security scanning, SBOM, pinned deps (Section 17 / Phase B). |

### 1.7 Entirely new V2 components (no V1 ancestor)

- `src/data/option_chains.py`, `broker_capabilities.py`, `catalysts.py` — chain normalization, runtime capability discovery, event calendar.
- `src/analytics/greeks.py`, `payoff.py`, `volatility.py`, `liquidity.py`, `opportunity_score.py`, `portfolio_exposure.py`, `stress.py`, opportunity-cost engine.
- `src/risk/trade_gate.py` (approval tokens), full kill-switch set.
- `src/execution/order_state_machine.py`, `reconciliation.py`, `idempotency.py`.
- `src/orchestration/session_controller.py` state machine, `health.py`, market calendar.
- Agents 2 and 4; property-based and chaos test suites; `docs/` set (this file, `SPEC_COMPLIANCE_REPORT.md`, `OPERATIONS_RUNBOOK.md`, `BROKER_CAPABILITIES.md`, `MODEL_CARD.md`).

---

## 2. Equity-specific assumptions inventory (must not leak into V2)

1. **Share-based sizing everywhere:** `shares = size_usd / price` (`portfolio_construction.py`), fractional shares, notional = shares × price (`execution.py`, `db.trade_journal.shares`). V2 sizes by *contract quantity from maximum loss* with 100× multipliers.
2. **Per-share price stops:** `HARD_STOP_LOSS_PCT` on underlying price (`risk/sizing.py`, `exit_monitor.py`). Meaningless for option premium; replaced by premium/underlying/time/vol/event exits.
3. **ATR as the volatility object:** `MarketSnapshot.atr_14`, vol-scalar sizing, scanner scoring. V2's volatility objects are IV, IV-vs-realized, term structure, skew, expected move.
4. **Symbol-level signals:** the entire V1 pipeline ranks *symbols*; V2 ranks *complete option structures* (Section 8.1 proposal schema).
5. **Long-only cash-account equity logic:** SHORT rejected outright (`portfolio_construction.py`); V2 expresses bearish views via long puts / defined-risk spreads.
6. **Share-volume liquidity floors:** `MIN_AVG_VOLUME=500_000` (`scanner.py`); V2 uses OI ≥ 100, contract volume ≥ 20, spread ≤ 12%, quote age ≤ 5 s.
7. **Static sector map / correlation proxy:** `SECTOR_MAP` in `risk/sizing.py`; superseded by exposure aggregation.
8. **Equity DB schema:** `trade_journal` (entry_price/shares/stop_loss_pct), `orders` (side/quantity), position valuation `price × shares` (`dashboard/api.py`).
9. **Take-profit as % of share price** (`strategy.py`, `exit_monitor.py`).
10. **OHLC-bar backtest fills** (gap-down stop fills, close marks) — cannot represent option pricing, spreads, or theta.
11. **Equity fundamentals channel** (`backtest/fundamentals.py`, `research/fundamental.py`) — revenue growth has no direct options-structure role.
12. **SPY buy-and-hold benchmark** in reports.
13. **Phase table keyed to equity account size** (`settings.PHASES`) — superseded by operating modes 0–4.
14. **`MAX_POSITION_PCT_OF_EQUITY=0.40` capital-fraction cap** — replaced by max-loss risk budgeting (1% per trade / 5% total).
15. **Float arithmetic for money** throughout V1 — V2 mandates `Decimal`.

---

## 3. Reusable infrastructure inventory

| Asset | Where | Reuse |
|---|---|---|
| Pure-code guardrail pattern + `HARD_GUARDRAIL_NAMES` + guardrail-drift test | `config/guardrails.py`, `test_guardrails.py` | Direct template for `risk_policy.py` |
| Settled-cash T+1 engine (`settled_cash`, `assert_purchase_is_covered`, `PaperPortfolio.settle`) | `risk/sizing.py`, `orchestrator.py` | Section 11 core, extended for debit/collateral |
| Model routing + failover chain + offline hermetic provider + failover-tagged decisions + "no model string outside config" test | `config/models.py`, `core/llm.py`, `test_model_layer.py` | Section 16 backbone |
| Agent 8 learning loop: calibration buckets, z-gates, shadow configs, human-gated promotion, guardrail-key assertion | `agents/auditor.py`, `agents/calibration.py` | Phase I backbone (re-dimensioned) |
| Broker Protocol + paper/live isolation + structurally-cannot-confirm live stub + tests | `agents/execution.py`, `test_execution.py` | Phase D interface ancestor |
| Read-only dashboard by construction + structural tests + event-bus WS relay | `dashboard/api.py`, `core/event_bus.py`, `test_dashboard.py` | Phase J |
| "Forced exits are code; LLM outage degrades gracefully" | `exit_monitor.py` + tests | Phase G |
| Hermetic offline modes (`TRADING_MARKET_DATA`, `TRADING_LLM`), deterministic fixtures | `core/market_data.py`, `core/llm.py`, `run_paper.py`, `conftest.py` | All test/paper phases |
| Point-in-time no-look-ahead store + walk-forward harness + A/B diff + cost metering | `backtest/*` | Section 19.4 + LLM budgeting |
| `Database` wrapper, `is_paper` tagging, config-version persistence | `core/db.py` | Phase B persistence |
| Tooling: ruff, strict mypy (py3.13), pytest | `pyproject.toml` | Phase B, extended |

---

## 4. Migration plan

### 4.1 Strategy

**In-place, additive migration on this repository** (git history preserves the V1 baseline at
`5ce3075`):

1. Build the V2 tree entirely under `src/` — including V2 configuration at `src/config/`
   (`models.py`, `risk_policy.py`, `strategy_registry.py`, `environments.py`, `tunables.py`)
   — **alongside** V1 modules. V1 files, including the top-level `config/` package, are not
   modified; nothing imports across the V1/V2 boundary. `src/config/` is lifted to the
   Section 21 top-level `config/` location in Phase K, once V1 is archived and the path is
   free.
2. Each phase lands with its tests green (`pytest`, `ruff`, `mypy`) before the next begins.
3. V1 modules are archived (moved to `legacy_v1/`, excluded from lint/type/test) only in
   Phase K, after the V2 paper lifecycle runs end-to-end — never before parity.
4. **PostgreSQL only, for the prototype and the final live system (locked decision
   2026-07-21).** Section 14 DDL runs verbatim — native UUID, TIMESTAMPTZ, JSONB, NUMERIC —
   with no dialect-lowering or compatibility shims. Alembic provides versioned, reversible
   migrations (one per Section 14 table); psycopg3 adapts NUMERIC↔`Decimal`. A fresh
   `options_v2` database is used; the V1 SQLite `trading.db` is never touched or migrated.
   The test suite runs against a real ephemeral Postgres so "pytest green" means green on
   the production engine. Connection string comes from `DATABASE_URL` (env / `.env`,
   never committed); services bind to localhost.

### 4.2 Phase sequence (per Section 22, executed in dependency order)

| Phase | Content | Key V1 carry-over |
|---|---|---|
| **B — Foundations** | Lockfile, security scanning, SBOM; `Decimal`-based domain models (`src/domain/*`); Section 14 migrations; immutable config versions + env gating; `config/risk_policy.py` with `PAPER_TRADING=True`, `ALLOW_LIVE_ORDERS=False`, `ORDER_MODE="research_only"` and guardrail-integrity tests | guardrail pattern, `Database` wrapper, tooling |
| **C — Deterministic analytics** | Chain normalization, Greeks (source-labeled), payoff/breakeven/max-loss, volatility service, liquidity/exec-cost, technical features, portfolio exposure, opportunity score + opportunity-cost engine, settled-cash/buying-power checks — all fully unit-tested | settlement engine, stats, market-data seams |
| **D — Broker adapters** | Typed Robinhood MCP adapter behind interface (inert until credentials; never fabricates a connection), capability discovery + snapshots, full paper broker, idempotent order state machine + reconciliation, every transition tested | Broker Protocol, paper/live isolation, `PaperPortfolio` |
| **E — Reasoning agents** | Nine agents, strict schemas, schema-repair-once-then-fail-closed, full `agent_decisions` logging | `ModelClient`, research base template, failover tagging |
| **F — Trade gate** | Ten-step precedence, approval tokens (short-lived, proposal/account/quote-bound), attempted-bypass tests for every guardrail | `RiskManager` ordered-checks pattern |
| **G — Position management** | Five-dimension exit plans, LLM-free emergency exits, DTE/assignment checkpoints, degraded no-new-entry mode | exit-monitor forced-exit ordering |
| **H — Orchestration** | Session state machine, startup validation, market calendar, health, event bus, event-driven workflows | event bus, orchestrator cycle patterns |
| **I — Learning & calibration** | Options-native buckets, MAE/MFE, slippage, Brier, shadow vs control, human promotion, rollback — fully implemented, not stubbed | Agent 8 auditor + calibration store |
| **J — Dashboard** | All Section 18 panels over read-only routes/WS; structural no-mutation tests | dashboard architecture + tests |
| **K — Validation** | Full quality gates; end-to-end paper session on fixtures; outage/partial-fill/restart/stale-quote/duplicate/settlement/drawdown simulations; `SPEC_COMPLIANCE_REPORT.md`, `OPERATIONS_RUNBOOK.md`, `BUILD_LOG.md`; archive V1 to `legacy_v1/` | backtest harness patterns, hermetic run drivers |

### 4.3 Invariants enforced from Phase B onward (tested, never relaxed)

1. `PAPER_TRADING=True`, `ALLOW_LIVE_ORDERS=False`, `ORDER_MODE="research_only"` — with a
   drift test that fails if any default changes.
2. No code path can place, fund, or confirm a live order (V1's structural-absence test
   pattern, re-applied to the MCP adapter).
3. No model string outside `config/models.py` / environment aliases (existing test pattern).
4. Dashboard read-only by construction (existing test pattern).
5. Agents never call broker tools; only the execution adapter consumes gate-issued approval
   tokens.
6. Stale data, unsupported strategies, and unsettled cash fail closed.

### 4.4 Known limitations to carry honestly

- **No live MCP credentials during the build:** the Robinhood adapter ships typed, mocked,
  and contract-tested with exact human setup instructions — never a fabricated connection.
- **No point-in-time historical options chains available locally:** historical simulation
  will be labeled *limited* per Section 19.4 and will not be used to justify autonomy.
- **Broker capability truth is unknowable until runtime discovery** against the real MCP:
  strategy availability defaults to the most restrictive assumption (single-leg only) until
  a capability snapshot proves otherwise.
