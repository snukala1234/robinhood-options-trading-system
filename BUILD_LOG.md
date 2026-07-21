# BUILD_LOG.md — 8-Agent Cash-Account Trading System

Continuous build log per Section 8 directive. Each entry records: what was built,
what failed, root cause, and fix. Guardrails were never weakened to make a step pass.

Governing constraints held for the entire build:
- `claude-fable-5` for every LLM-backed agent, resolved only through `config/models.py`.
- `PAPER_TRADING = True`, `ORDER_APPROVAL_MODE = "manual"` throughout. No live-order path.
- Section 0 hard limits and the Section 1.1 settled-cash / GFV invariant are inviolable.

---

## Environment setup

**Built:**
- `python -m venv .venv` (Python 3.13.1).
- Installed: pytest 9.1.1, ruff 0.15.20, mypy 2.1.0, fastapi 0.139.0, uvicorn[standard] 0.50.0,
  httpx 0.28.1, anthropic 0.116.0, yfinance 1.5.1, pandas 3.0.3, numpy 2.5.1, python-dateutil.
- Froze fully-pinned lockfile → `requirements.txt` (60 pins).
- `pyproject.toml`: ruff (E/W/F/I/UP/B/SIM/C4), mypy (disallow_untyped_defs, ignore_missing_imports),
  pytest config.

**Environment deviations (confirmed with user, see plan):**
- node/npm not installed → dashboard is React-via-CDN served by FastAPI (no build toolchain).
- git not installed → no VCS; lockfile satisfied via pinned `requirements.txt`.
- Real Robinhood MCP + real claude-fable-5 API need user credentials (Section 8.2 defers to human)
  → built behind interfaces with offline/paper adapters as default; real wiring auto-activates
  when credentials present. No live-order path exists in the build.

**Failures/fixes:** none — pip resolved all deps on Python 3.13 first try.

---

## Component 1 — Data & persistence layer

**Built:**
- `core/util.py` — UUID + UTC ISO-8601 timestamp helpers (single source of truth for time).
- `core/records.py` — shared dataclasses: `MarketSnapshot`, `ResearchSignal`,
  `AggregatedSignal`, `Position`.
- `core/db.py` — `Database` over SQLite. All six Section 4 tables (`trade_journal`,
  `calibration_buckets`, `strategy_config_versions`, `shadow_test_results`,
  `equity_snapshots`, `daily_pnl`) plus four spec-required support tables:
  `signal_history` (Section 2/6-step-2: persist every research output even with no trade),
  `orders` (Section 7.5 display mirror), `agent_status` (Section 7.1 last-event-per-agent),
  `model_failover_events` (Section 3.8 audit). Typed domain methods for every table.
- `core/market_data.py` — yfinance OHLCV + ATR-14 + volume-vs-avg, with a *deterministic*
  offline fixture fallback (`TRADING_MARKET_DATA=offline`) so paper runs are hermetic and
  reproducible. Also `get_market_regime()` (Section 3.4, independent of trade outcomes).
- `core/event_bus.py` — in-process pub/sub (Section 7.1); sync subscribers + asyncio-queue
  bridge for the WebSocket relay; publish is best-effort and never raises into callers.
- `core/logging_setup.py` — structured logging + `log_decision()` (Section 6 "reconstruct why").
- `tests/test_data_layer.py` — 13 tests (schema presence/idempotency, trade lifecycle,
  signal-without-trade persistence, equity/daily-pnl, agent-status upsert, failover audit,
  deterministic market data, event-bus sync+async).

**Failed → root cause → fix:**
1. `test_offline_snapshot_is_deterministic` failed on full-object equality. Root cause:
   `MarketSnapshot.as_of` is the real fetch time, legitimately non-deterministic; only the
   market fields are deterministic. Fix: compared the market fields, not `as_of` (fixed the
   test, not the feature).
2. ruff `SIM118` on `row.keys()` in `_row_to_dict`. Root cause: false positive —
   `sqlite3.Row` iterates *values*, so `.keys()` is required. Fix: targeted `# noqa: SIM118`
   with an explanatory comment.

**Result:** ruff clean, mypy clean (9 files), 13/13 tests pass, runtime smoke OK.

---

## Component 2 — Guardrails + sizing (tests-first)

**Built (guardrail-violation tests written first, then the feature):**
- `config/guardrails.py` — Section 0 hard limits as the single source of truth
  (`HARD_STOP_LOSS_PCT=0.18` within the 0.15–0.20 band, `MAX_POSITION_PCT_OF_EQUITY=0.40`,
  `MAX_CONCURRENT_POSITIONS=3`, `MAX_DAILY_LOSS_PCT=0.10`, `MAX_DRAWDOWN_HALT_PCT=0.25`,
  `MIN_SIGNAL_CONFIDENCE_TO_TRADE=0.65`, `ORDER_APPROVAL_MODE="manual"`, `PAPER_TRADING=True`,
  `ENFORCE_SETTLED_CASH_ONLY=True`, `MIN_SAMPLE_SIZE_FOR_ADAPTATION=30`). `HARD_GUARDRAIL_NAMES`
  frozenset lets tests/Agent 8 prove the tunable set never overlaps a guardrail.
- `config/strategy.py` — `StrategyParams`: the ONLY knobs Agent 8 may adapt. Defaults equal
  the Section 1 pseudocode literals; `clamp_to_ranges()` bounds every param to a pre-approved
  range so adaptation can never escape its lane.
- `risk/sizing.py` — Section 1 & 1.1 implemented verbatim in pure code:
  `calculate_position_size` (caps at BOTH settled cash and equity*0.40, concurrency gate,
  volatility/confidence/correlation scalars), `check_stop_loss` (forced), `check_portfolio_halt`
  (daily-loss + drawdown), `settled_cash`, `assert_purchase_is_covered` (GFV backstop reading
  `ENFORCE_SETTLED_CASH_ONLY` live), `sector_correlation`, `passes_confidence_gate`.
- `tests/test_guardrails.py` — 18 tests, each attempts a violation and asserts a block:
  position-% cap, settled-cash cap, zero-settled-cash, max concurrent, confidence gate,
  forced hard stop (at/beyond/above threshold), daily-loss halt, drawdown halt, settled-cash
  excludes unsettled proceeds, GFV purchase blocked, and the end-to-end structural guarantee
  that any size-derived order is always covered by settled cash.

**Failed → root cause → fix:**
- ruff `C416`/`UP017` style nits (set-comprehension → `set()`, `timezone.utc` → `datetime.UTC`).
  Cosmetic; fixed directly. No guardrail was touched.

**Result:** ruff clean, mypy clean (13 files), 31/31 tests pass, sizing smoke shows the
equity cap binding ($142.86 ≤ $200).

---

## Component 3 — Model layer + failover (Sections 3.7, 3.8)

**Built:**
- `config/models.py` — `AGENT_MODELS` (all 10 agent keys → `claude-fable-5`), `FAILOVER_CHAIN`
  (fable-5 → opus-4-8 → sonnet-5), `MAX_MODEL_UNAVAILABLE_RETRIES=3`, and `model_for` /
  `failover_model` / `model_chain` (cycle-safe). The single place any model string exists.
- `core/llm.py` — `ModelClient.complete_json(agent_key, system, user, offline_result)`.
  Pluggable provider: `AnthropicProvider` (maps SDK errors to transient vs sustained),
  `OfflineProvider` (hermetic default; returns the caller's deterministic offline_result).
  Live path walks the failover chain: transient errors retry the same model up to the retry
  cap, sustained unavailability hops to the next model; each hop is logged to
  `model_failover_events` + the event bus, and the decision is tagged `decided_under_failover`.
  `extract_json` robustly parses JSON out of fenced/prose responses.
- `tests/test_model_layer.py` — 12 tests: all agents default to Fable 5, unknown-key raises,
  chain order, offline path, sustained→failover (tagged + audited), transient retry (no
  failover), transient-exhaustion escalation, all-unavailable raises, JSON extraction, and a
  **spec-fidelity scan asserting no `claude-fable-5` literal exists outside `config/models.py`**.

**Failed → root cause → fix:**
1. `test_sustained_...failover` saw 2 audit events, expected 1. Root cause: a real
   double-logging bug — the client logged a failover both on the hop AND again on success
   under the fallback model. Fix: record each failover only at the hop; the success path just
   tags the result. (Removed the now-unused `PRIMARY_MODEL` import.)
2. mypy `union-attr` on `block.text` (Anthropic content-block union). Fix: `getattr(block,
   "text", "")` guarded by the block type — no behavioural change.

**Result:** ruff clean, mypy clean (15 files), 43/43 tests pass, routing + offline smoke OK.

---

## Component 4 — Research agents (Agent 2) + Edge Aggregator (Agent 3)

**Built:**
- `agents/calibration.py` — confidence banding (Section 3.2) + `RecalibrationStore`
  (per-agent/per-band additive correction, Section 3.3), identity until Agent 8 has data.
- `agents/research/base.py` — `ResearchAgent` pipeline: deterministic offline heuristic
  (model stand-in) → Fable-5 call via `ModelClient` → recalibrate → persist to
  `signal_history` (every output, trade or not) → publish status + signal-flow events.
- Four sub-agents: `technical` (volume/ATR read), `fundamental` (quality proxy),
  `sentiment` (tone proxy), `macro` (regime-driven). Each returns direction/magnitude/
  raw+calibrated confidence/reasoning as structured JSON.
- `agents/edge_aggregator.py` — deterministic ensemble math in pure code (Fable 5 used only
  for the synthesis *narrative*, per the cross-cutting "no LLM for arithmetic" rule);
  ensemble weights default equal and are the Section 3.3 lever Agent 8 tunes; persists the
  aggregated signal; propagates the failover flag if any contributor was on a fallback model.
- `tests/test_research.py` — 7 tests: valid signals for all four agents, offline determinism,
  persist-without-trade, recalibration shifts calibrated confidence, aggregator combines four
  signals + carries the market read, confidence dampened by disagreement, failover propagation.

**Failed → root cause → fix:**
1. No universe symbol crossed the 0.65 trade gate (max 0.587). Root cause: a *modelling*
   error in the aggregator — agreement was measured against total weight including FLAT
   (abstaining) agents, so abstentions were counted as disagreement and over-dampened
   confidence. Fix: measure agreement among directional voters only (denominator = long+short
   weight); FLAT is abstention, not opposition. A genuine long-vs-short split is still
   heavily dampened (the disagreement test still passes at 0.45). The 0.65 guardrail was NOT
   touched. After the fix, AMZN long 0.783 / NVDA short 0.741 / MSFT short 0.705 cross the
   gate; only the long is tradeable on a cash account (long-only enforced in Component 6).

**Result:** ruff clean, mypy clean (24 files), 50/50 tests pass, universe scan yields a
tradeable long candidate.

---

## Component 5 — Scanner (Agent 1)

**Built:** `agents/scanner.py` — pure-code liquidity prefilter (`MIN_AVG_VOLUME`, an
operational filter, not a risk guardrail) + Fable-5 ranking of which liquid names are worth
deeper research (deterministic offline score stand-in). Returns ranked `ScanCandidate`s,
publishes status + signal-flow events. `tests/test_scanner.py` — 4 tests (ranked output,
max-candidates cap, liquidity filter, offline determinism).

**Failed → fix:** none.

**Result:** ruff clean, mypy clean (25 files), 54/54 tests pass.

---

## Component 6 — Portfolio Construction (Agent 4) + Risk Manager (Agent 5)

**Built:**
- `agents/portfolio_construction.py` — `PortfolioConstruction.build()` returns a
  `TradeProposal`. Sizing is PURE CODE via `risk.sizing.calculate_position_size` (no LLM on
  the dollar amount). Enforces cash-account **long-only** (short → rejected), the Section 0
  confidence gate, and the concurrency/settled-cash caps (size 0 → rejected). Fable 5 is used
  only for a non-authoritative construction rationale, routed through `config.models`.
- `agents/risk_manager.py` — `RiskManager.evaluate()` is the code-first final gate. Approve/
  block depends ONLY on pure-code checks: portfolio halt (drawdown/daily-loss), proposal
  viability, confidence gate, positive size, and the Section 1.1 `assert_purchase_is_covered`
  settled-cash backstop. Fable 5 produces only a non-authoritative narrative flag that can
  never flip the decision.
- `tests/test_portfolio_risk.py` — 10 tests: viable long built; short/low-confidence/
  concurrency rejected; risk manager approves valid long; blocks on drawdown halt, daily-loss
  halt, and the unsettled-funds backstop (oversized proposal blocked independently of sizing);
  propagates construction rejection; narrative never flips the decision.

**Failed → fix:** none.

**Result:** ruff clean, mypy clean (27 files), 64/64 tests pass.

---

## Component 7 — Execution (Agent 6, Robinhood MCP)

**Built:** `agents/execution.py` — code-first, no LLM.
- `Broker` protocol has only `stage` + `submit_for_approval` — **no confirm/fill method**.
- `RobinhoodMCPBroker` (live) can only stage/submit; it structurally has no way to confirm an
  order (the human approves in the Robinhood app), requires an explicit MCP transport + a
  dedicated agentic account id, and its staging is disabled in the paper build.
- `PaperBroker` simulates the full approve+fill lifecycle (tagged `is_paper=True`) for testing.
- `ExecutionAgent.place_entry` re-runs the Section 1.1 settled-cash backstop before staging
  (blocks unsettled-funds buys); in paper mode simulates the fill; in live mode stops at
  `submitted_for_approval`. `place_exit` sells with no settled-cash guard (Section 1.1: selling
  a settled-cash-bought position can't create a GFV). Orders recorded to the `orders` mirror
  with `approval_mode="manual"`; one row per order, status advanced via `update_order_status`.
- `tests/test_execution.py` — 7 tests: paper entry stages+fills, paper entry blocked on
  unsettled funds, paper exit fills, **live mode never confirms (only awaits Robinhood)**,
  live broker has no confirm/approve/fill attribute, live broker requires transport+account,
  live staging disabled in the paper build.

**Failed → fix:** none (added `db.update_order_status` so an order is one row, not one-per-transition).

**Result:** ruff clean, mypy clean (28 files), 71/71 tests pass.

---

## Component 8 — Exit Monitor (Agent 7)

**Built:** `agents/exit_monitor.py` — pure-code hard stop-loss + take-profit evaluated FIRST
on every poll tick (a forced exit never depends on an LLM), then Fable-5 thesis-invalidation.
Per Section 3.8 it has the shortest failover tolerance: `_thesis_invalidated` catches
`AllModelsUnavailableError` and falls back to pure-code stops only, never leaving a position
unmonitored. Exit taxonomy: stop_loss / take_profit / thesis_invalidated / scheduled_review.
`tests/test_exit_monitor.py` — 7 tests: forced stop, take-profit, hold-with-intact-thesis,
thesis-invalidation exit, model-outage falls back to pure code (no raise), model-outage still
forces the stop (pure code runs before any model call), multi-position tick.

**Failed → fix:** none.

**Result:** ruff clean, mypy clean (29 files), 78/78 tests pass.

---

## Component 9 — Agent 8 learning loop (FULL, not stubbed)

**Built:**
- `core/stats.py` — pure stats primitives: `proportion_z_score`, `is_significant` (|z|>=1.96),
  `sharpe_ratio`, `welch_t`, `difference_is_significant`.
- `agents/auditor.py` — `CalibrationAuditor` implementing Section 3 end to end:
  - **3.2** `compute_buckets` (per contributing agent + aggregator × confidence band, persisted
    to `calibration_buckets`), `evaluate_calibration` (exact pseudocode: sample-size gate →
    significance gate → reduce/increase proposal).
  - **3.3** `build_shadow_parameters` translates proposals into TUNABLE-only params
    (recalibration deltas, ensemble re-weighting — down-weight never remove, clamped strategy).
  - **3.4** `detect_regime` + `regime_risk_nudge` (dampens tunable scalars in volatile regimes),
    kept separate from calibration drift.
  - **3.5** `register_shadow_config` (is_shadow), `evaluate_config`/`control_result` (paper
    backtest of selection), `promote_shadow_config` (Sharpe + Welch significance + min sample).
  - **3.6** human checkpoint: promotion needs an explicit confirmer; default `auto_hold_confirmer`
    HOLDs. `_assert_no_guardrail_keys` guarantees Agent 8 can never write a Section 0 guardrail.
  - Fable-5 narrative (`summarize_findings`) for pattern-finding — non-authoritative.
- `tests/test_auditor.py` — 11 tests: significant overconfidence → reduction, insufficient
  sample blocks, within-variance no-action, shadow params are tunable-only, guardrail-key
  registration rejected, promotion requires human yes (declined→hold / yes→activates new
  human_confirmed config), insufficient shadow sample holds, no-improvement holds, regime
  nudge, end-to-end audit registers a shadow config + persists buckets.

**Failed → root cause → fix:**
1. mypy over `object`-typed accumulator dicts (`int(obj)`/`float(obj)`). Fix: typed `_Acc`
   dataclass + `_as_float` coercion helper.
2. Invalid conditional-import placeholder left in the import block. Fix: removed it, trimmed to
   the four stats functions actually used.
3. mypy (full, incl. tests): list-of-abstract-classes loop, generator fixture return type,
   `a.db` typed `object`. Fix: iterate concrete instances (`build_all`), `Iterator[Database]`
   fixture type, use the `db` fixture directly for `active_config()`.

**Result:** ruff clean, **mypy clean across all 43 files (incl. tests)**, 89/89 tests pass.

---

## Component 10 — Orchestrator + run_paper entrypoint

**Built:**
- `orchestrator.py` — `PaperPortfolio` (settled cash + unsettled T+1 sale proceeds + HWM;
  `settle()` matures proceeds) and `Orchestrator` wiring Scanner → Research(4) → Aggregator →
  Construction → Risk → Execution, plus Exit Monitor and Agent 8. `run_entry_cycle` (one
  position per symbol, concurrency cap, **Section 3.8: skips entries decided under failover**),
  `run_monitor_cycle` (settles, exits, records sells as unsettled proceeds), `snapshot_equity`
  (writes `equity_snapshots` + live tick), `rollup_daily_pnl` (writes `daily_pnl`), `run_audit`.
  Bootstraps an active default config; persists agent status to `agent_status` via the bus.
- `run_paper.py` — hermetic entrypoint; deterministic oscillating price path exercises the full
  open→monitor→close lifecycle; prints a summary + Agent 8 audit.
- `db.mark_signal_traded` + `db.update_order_status` support methods.
- `tests/test_orchestrator.py` — 8 tests: portfolio settlement excludes unsettled then settles,
  buy reduces settled cash, entry opens + snapshots + config attached + signal marked traded,
  no entry without settled cash, monitor stop-loss close (proceeds unsettled), take-profit close,
  **new entries disabled under failover (audited)**, daily rollup written.

**Failed → root cause → fix:**
1. First run opened AMZN but never closed and re-entries would use the stale snapshot price.
   Root cause: entries priced off the static snapshot, not the live price. Fix: thread a single
   `price_of` through entry/exit/equity; enter at the live price.
2. Random price walk stayed bounded by luck → no exits. Root cause: independent per-session
   noise mean-reverted. Fix: deterministic oscillating (±33%) paper price path that reliably
   crosses both the +25% take-profit and −18% stop (a price *simulator*, not strategy logic).

**Result:** `python run_paper.py` → 5 opened / 4 closed (2 TP wins, 2 stop losses), 5 calibration
buckets, Agent 8 correctly HOLDs (n<30). ruff clean, mypy clean (46 files), 97/97 tests pass.

---

## Component 11 — Dashboard (read-only FastAPI + React-via-CDN)

**Built:**
- `dashboard/api.py` — read-only FastAPI. GET endpoints: `/`, `/api/agents/status`,
  `/api/guardrails`, `/api/equity?range=…`, `/api/calendar?month=…`, `/api/day/{date}`,
  `/api/pnl/summary` (server-side today/week/month/year rollups), `/api/positions/open`
  (marked to market), `/api/orders/recent` (display-only mirror). Read-only WS `/ws/agents`
  and `/ws/equity` fed by the in-process event bus (send an initial DB snapshot on connect).
  No import of the execution/MCP layer; no state-mutating route exists.
- `dashboard/static/index.html` — React 18 via CDN (no build toolchain), theme-aware, four
  panels: A live agent board + guardrail strip + signal-flow line, B equity area chart (inline
  SVG) with HWM line + range selector + PAPER badge, C calendar heatmap with month nav + day
  detail, D P&L breakdown cards with sparklines. Persistent paper/live + failover banners;
  panels show a "stale" outline on fetch failure (fail-visible).
- `tests/test_dashboard.py` — 10 tests: **no state-mutating routes**, POST/PUT/DELETE rejected,
  **dashboard imports no execution/MCP code**, every endpoint serves seeded data, index served,
  WS initial snapshot.

**Failed → fix:** one mypy nit (`BaseRoute.path`) in a test assertion message → `getattr`.

**Runtime check:** launched `uvicorn dashboard.api:app` — `/`, `/api/guardrails`, `/api/equity`,
`/api/pnl/summary` all HTTP 200 reading the paper-run DB (paper_trading=True, approval=manual).

**Result:** ruff clean, mypy clean (48 files), 107/107 tests pass.

---

## FINAL SPEC-FIDELITY PASS + Section 8.1 Definition of Done

Re-read Sections 0–8 and verified point by point. Final state: **ruff clean, mypy clean
(48 files), 107/107 tests pass**, dashboard serves live over uvicorn, `run_paper.py` runs the
full pipeline end-to-end in paper mode.

### Section 8.1 checklist

- [x] **Every component in Section 6 order exists, runs, wired end-to-end.** data/persistence →
  research+aggregator → risk/sizing (pure code) → execution (MCP) → exit monitor → Agent 8 →
  orchestrator → GUI. `run_paper.py` drives all of it.
- [x] **All agents route model calls through `config/models.py`, default `claude-fable-5`;
  failover wired + unit-tested.** Agents 1, 2(×4), 3, 4, 5 (narrative), 7, 8 call via
  `ModelClient`. Agent 6 (Execution) is code-first with NO model call — exactly per the Section 2
  table ("Code-first (MCP client) — No reasoning needed"). A test asserts no model literal exists
  outside `config/models.py`; failover chain has 12 unit tests.
- [x] **Agent 8 fully implemented — not stubbed.** Buckets, z-score gate, shadow config, Sharpe+
  Welch promotion, human checkpoint, regime detection. 11 tests.
- [x] **Full suite passes with explicit guardrail-violation tests (Section 0 + 1.1).**
  `tests/test_guardrails.py` (18) + settled-cash backstop tests each *attempt* a breach and assert
  a block. No guardrail test is skipped/xfail'd.
- [x] **Linter and type checker clean.** ruff + mypy(48) clean.
- [x] **All six Section 4 tables + equity_snapshots + daily_pnl created and written.** Row counts
  after a fresh `run_paper.py`: trade_journal 5, calibration_buckets 10, strategy_config_versions 1,
  equity_snapshots 33, daily_pnl 14, signal_history 515, orders 9, agent_status 12.
  `shadow_test_results` and `model_failover_events` are 0 in the short paper demo (no bucket cleared
  the 30-sample gate; no model outage offline) — both are written by their components and verified
  by tests. This 0 is the *correct* Phase-1 behaviour the spec describes (Section 6 / 8.2).
- [x] **All four GUI panels render from real backend data; no state-mutating endpoint.** A test
  enumerates routes and asserts only GET/HEAD/OPTIONS; another asserts the dashboard imports no
  execution/MCP code. Verified live over uvicorn.
- [x] **Runs with `PAPER_TRADING = True` and `ORDER_APPROVAL_MODE = "manual"`; no reachable live
  order path.** The live broker has no confirm/fill method at all; execution stops at
  `submitted_for_approval` in live mode. Tests prove it.
- [x] **No TODOs / placeholder returns / pass-only stubs / dead code in shipped components.**
  Scanned: the only `pass` statements are legitimate control flow (JSON parse fallthrough; clean
  WebSocket disconnect).
- [x] **BUILD_LOG.md reconstructs the build; final report present** (this section + the summary
  returned to the user), including deviations and the exact paper-mode launch command.

### Deviations from spec (with reasons)

1. **Dashboard is React via CDN, not a compiled npm SPA** — node/npm is not installed in the
   environment. Same read-only React SPA behaviour with zero build toolchain, served by FastAPI.
2. **SQLite, not Postgres** — spec permits "Postgres/SQLite"; correct for local Phase-1 single user.
3. **Offline/paper adapters are the default for the Robinhood MCP and the Fable-5 API** — real
   endpoints require the user's credentials, which Section 8.2 explicitly defers to the human. Both
   are built behind clean interfaces; real wiring auto-activates when credentials are present. No
   live-order path exists in the build.
4. **`HARD_STOP_LOSS_PCT = 0.18`** — the spec gives a 0.15–0.20 band; a single concrete value is
   required in code. 0.18 (mid-band) is used; a test asserts it stays within the band.
5. **No git VCS** — not installed; the "lockfile" requirement is met by a fully-pinned
   `requirements.txt`.

None of these weaken a guardrail or alter any Section 0 / Section 1.1 invariant.

### Exact command to launch in paper mode

```
.\.venv\Scripts\python.exe run_paper.py
```

Dashboard: `.\.venv\Scripts\python.exe -m uvicorn dashboard.api:app --host 127.0.0.1 --port 8000`

---

## ADDENDUM — Backtest harness (real historical data, no look-ahead)

**Built (harness only; no change to agent/guardrail/sizing/exit logic):**
- `core/market_data.py` — added a point-in-time snapshot-provider seam (`set_snapshot_provider`
  / `clear_snapshot_provider`), a no-op when unset. This is the sanctioned "replace the data
  source" hook; it lets the real pipeline see as-of-date snapshots only.
- `backtest/data.py` — `HistoricalBarStore`: real daily bars via yfinance (cached to
  `data/backtest_cache/`), `snapshot_asof` (bars ≤ date only → **no look-ahead**), `bar_on`
  (one day's OHLC), trading calendar, regime proxy.
- `backtest/engine.py` — walk-forward driver. Entries go through the real
  `Orchestrator.run_entry_cycle` on as-of snapshots; exits are detected by the real
  `ExitMonitor`/Section-1 stop logic but evaluated against each day's ACTUAL OHLC (low for
  stops, high for take-profit) with slippage + gap fills; T+1 settlement + sizing come from the
  unchanged `PaperPortfolio`/`risk.sizing`. Offline LLM (no API tokens), PAPER_TRADING=True.
- `backtest/report.py` — total return, trades, win rate, avg win/loss, max drawdown, Sharpe,
  calibration (reusing `CalibrationAuditor` bucketing), by-year and by-regime breakdowns,
  SPY benchmark, and honesty caveats.
- `run_backtest.py` — 20-symbol universe + SPY, 2022-01→2026-07 (~4.5y), $2,000, 5 bps/side.
- `tests/test_backtest.py` — 6 tests incl. **snapshot ignores future bars**, **stop fires on the
  breach day not before (no look-ahead)**, buy slippage applied, T+1 proceeds unsettled,
  determinism.

**Result (real data, 2225 trading days):** total return +30.2% vs SPY buy&hold +76.0%
(strategy LAGGED passive by ~46 pts); Sharpe 0.64; max DD 9.7%; 20 closed trades, 75% win.
By-year: 2022 (bear) −$77 / 0% win, 2023 +$232, 2024 +$333, 2025 +$72 → result concentrated in
the 2023-24 bull, lost in the 2022 bear (regime-dependent). All calibration buckets n<30 (not
significant). Honesty flags surfaced in the report: offline heuristic signals (not real Fable 5;
fundamental+sentiment are noise offline), survivorship bias, long-only, small sample.

ruff clean, mypy clean (54 files), **113/113 tests pass**.

---

## ADDENDUM 2 — Backtest efficiency fixes + real-fundamentals comparison

**Efficiency fixes (backtest-only, behaviour-preserving; core untouched):**
- Fix 1: `BacktestConfig.thesis_enabled=False` (default) — engine swaps in a thesis-disabled
  `ExitMonitor`. Pure-code stop/take-profit still closes trades. `test_thesis_toggle_does_not_
  change_closes` proves the closed-trade set is identical with thesis on vs off.
- Fix 2: `skip_scan_when_full` — engine skips the entire entry cycle (incl. the scanner LLM
  call) on days already at `MAX_CONCURRENT_POSITIONS`.
- Effect on the 5-symbol slice: **1,837 → 1,093 model calls** (exit_monitor 703→0, scanner
  98→57). On the full 20-symbol run the reduction is far larger (exit_monitor was ~85% of calls).

**Real fundamentals + neutralized sentiment:**
- `backtest/fundamentals.py` — `FundamentalsStore`: real yfinance quarterly revenue, cached,
  with a 45-day filing lag; `growth_asof` returns YoY (or QoQ fallback) using only quarters
  public as-of the date. Point-in-time proven by `test_fundamentals_respect_reporting_lag`.
- `backtest/variants.py` — `RealFundamentalAgent` (drives off real revenue growth via
  `ctx.snapshot.as_of`) and `NeutralResearchAgent` (flat; used for sentiment). Injected into the
  backtest orchestrator via `BacktestConfig.fundamental_mode` / `sentiment_mode`. Core agents
  untouched.
- `backtest/compare.py` + `compare_slice.py` — signal-level probe (same symbol×day grid under
  both configs) + trade-level diff.

**Comparison result (5 symbols, 2026-01→2026-07):** real fundamentals materially changed the
SIGNALS — fundamental direction differed on 294/490 symbol-days (60%), aggregated direction on
168 (34%), and the 0.65 gate-crossing flipped on 152 (31%); mean |Δconf| 0.15. But the executed
TRADES barely changed (baseline 4 vs variant 4, 3 common; one position differed: GOOGL→MSFT) —
the 3-position cap + settled-cash bottleneck limits how many signal changes become trades. Both
variants ~flat over 6 months vs SPY +8% (tiny sample: 1 closed trade each).

**Honest blockers (surfaced in the report):** live `claude-sonnet-5` NOT run (no
`ANTHROPIC_API_KEY`); real historical news sentiment unobtainable (yfinance `.news` is
current-only, untimestamped) → sentiment neutralized, not real.

ruff clean, mypy clean (61 files), **119/119 tests pass**.

