# V2 Build Log

Failures, fixes, and notable decisions per phase (finalized in Phase K).
V1's build log remains at the repo root (`BUILD_LOG.md`) and is not modified.

## Phase A — Discovery and migration plan (2026-07-21)

- Produced `docs/V1_TO_V2_TRACEABILITY.md` (traceability matrix + migration plan).
- Decision: all V2 code, config included, lives under `src/`; V1 files untouched until
  Phase K archival. Resolves the `config/models.py` name collision with Section 21.
- **Locked decision:** PostgreSQL only — prototype, paper, and live. No SQLite in V2.

## Phase B — Foundations (2026-07-21)

Delivered: `src/config/` (risk_policy verbatim from Section 3 with drift test, model
alias routing, strategy registry, tunables, environment gating), `src/domain/`
(Decimal-only money, frozen dataclasses, Section 12.2 order-state vocabulary,
five-dimension exit plans), `src/persistence/` (psycopg3 connection layer, immutable
config-version repository, 14 Alembic migrations running the Section 14 DDL verbatim),
docker-compose Postgres (localhost-bound), `.env.example`, `requirements.lock`,
pip-audit scan (clean), and 63 V2 tests running against a real ephemeral Postgres
(testcontainers). Full suite: 170 passed; ruff and strict mypy green over 102 files.

Failures encountered and fixed:

1. **mypy `.exe` shim silently broken** after installing the new dependency set — it
   exits 1 with no output, even for `--version`. Workaround: invoke as
   `python -m mypy` (works correctly). Root cause not chased; noted for Phase K
   validation scripts to always use the module form.
2. **PowerShell 5.1 `Get-Content`/`Set-Content` round-trip mojibake:** a bulk regex
   edit decoded UTF-8 test files as ANSI, corrupting em-dashes (`—` → `â€”`).
   Fixed by hand; lesson: use targeted editor edits, not shell round-trips, for
   files containing non-ASCII.
3. **mypy dataclass plugin checks `dataclasses.replace`** (typed kwargs), so the
   intentionally-wrong-type test needed an explicit `# type: ignore[arg-type]`;
   `TunableParams.from_dict` needed `dict[str, Any]` because the snapshot mixes
   float and int fields.

## Phase C — Deterministic analytics (2026-07-21)

Delivered: `src/data/option_chains.py` (normalization with provenance/freshness,
`StaleQuoteError`), `src/analytics/` (Greeks with BROKER/CALCULATED source labels and
recorded assumptions; payoff/breakeven/max-loss with undefined-risk rejection;
volatility term structure/skew/expected move/realized vol in pure Decimal via
`Decimal.ln`/`Decimal.sqrt`; liquidity + execution-cost estimates against policy
floors; technical feature service; portfolio dollar-Greeks, limit headroom, and
delta-gamma stress floored at defined max loss; opportunity score with stored
components; opportunity-cost engine with budget/cash/quality gates and ranking), and
`src/risk/settlement.py` (settled-cash/collateral checks, exits never blocked).
75 new tests with hand-verified values (spec's own 600/605 spread: 185/315/601.85;
Black-Scholes reference at S=K=100, r=5%, sigma=20%, t=0.2y) and explicit
stale/invalid-input rejection tests. Full suite 245 green; mypy/ruff clean.

Notes: Black-Scholes transcendental math runs in floats internally (documented —
Greeks are model estimates, not money) and quantizes to 6 dp with assumptions
recorded; all money arithmetic remains pure Decimal. One fix: a test asserted the
`None` return of an always-`None` function (mypy `func-returns-value`) — rewritten
to assert "no exception".

## Phase E — Nine reasoning agents (2026-07-22)

Delivered: `src/agents/` — strict Pydantic output contracts for all nine Section 6
agents (`schemas.py`, extra="forbid", cross-field model validators, decimal-string
money, registry-validated strategies, guardrail names structurally unproposable);
shared `runtime.py` (alias-only model resolution, offline hermetic mode through the
same schemas, transient-retry + sustained-failover with `decided_under_failover`
tagging, exactly one schema-repair retry then fail closed, full `agent_decisions`
logging incl. failures, `agent_unavailable` system events +
`agent_unavailability_blocks_entries` for REQUIRED_ENTRY_AGENTS, and
`failover_blocks_new_entry` enforcing ALLOW_NEW_ENTRY_DURING_FAILOVER=False);
`untrusted.py` prompt-injection hygiene (fence, neutralize, flag) on top of the
structural defense that agents have no tools at all; and the nine agent modules,
each with an immutable PROMPT_VERSION, a frozen validated feature packet, a pure
deterministic offline rule set, and (strategy selector) a semantic
executable-strategies gate after schema validation. Catalyst Researcher derives
catalysts only from the trusted calendar; news text is fenced and can only raise
`suspicious_content_detected`. 90 new tests (repair/fail-closed paths, failover
tagging, entry-blocking, injection attempts, per-agent rule branches with exact
expected outputs, DB decision logging). Full suite 382 green; mypy/ruff clean.

Notes: the user declined a multi-agent Workflow fan-out for this phase mid-build;
implementation completed inline. One fix: ruff UP046/UP047 required PEP 695
generic syntax for `AgentCallResult`; one stale `type: ignore` removed.

## Phase F — Deterministic trade gate and approval tokens (2026-07-22)

Delivered: `src/gate/` — `kill_switches.py` (all eleven Section 3.2 switches
plus the four step-7 circuit breakers on one panel; monotonic halt epoch bumped
by every activation AND every manual resume; clearing requires an identified
human per REQUIRE_MANUAL_RESUME_AFTER_HALT; changes logged as critical
system_events); `committee.py` (Risk Officer veto terminates the proposal at
the gate boundary; combined reductions take the MINIMUM; >1 fractions are
unrepresentable in the schemas and rejected again by sizing); `trade_gate.py`
(the exact Section 3.1 ten-step precedence, short-circuiting at the first
failure with later steps recorded `not_evaluated` — downstream can never
override upstream; Section 9 sizing with reduce-only committee fraction;
`ApprovalToken` mintable ONLY by the gate via a module-private capability,
short-lived (30s) and bound to proposal + account-state hash + quote-snapshot
hash + halt epoch; every evaluation upserted to `trade_proposals` keyed by
proposal id). `src/risk/sizing.py` (Section 9 `calculate_contract_quantity`
verbatim + reduce-only `risk_fraction`). `src/execution/submission.py`
(`OrderSubmitter`, the single src call site of `.submit_order(`: verifies
token type/expiry/proposal/price/quantity/account-hash/quote-hash, then
re-reads the live kill-switch panel IMMEDIATELY before broker submit and fails
closed on any active switch or epoch drift; tokens single-use; orders walk
CREATED→VALIDATED→STAGED→SUBMITTED through the event-sourced machine).

72 new tests (454 total): attempted-bypass per guardrail step (stale/future
quotes, kill switches, reconciliation uncertainty, failover, stale
account/underlying data, capability-gated strategy, price increment,
settled-cash shortfall incl. fees, credit collateral max(broker, own), DTE
bounds, earnings, contract-price cap, underlying concentration, concurrent
positions, all four circuit breakers, wide spread, thin OI, live destination
always refused), out-of-order-precedence impossibility, token
forge/replay/expiry/reuse, the audit-finding-1 integration (token valid at
issuance dies when a switch trips before submit and STAYS dead after manual
resume because the epoch moved twice), veto-never-yields-token,
smaller-reduction-wins, hypothesis property (sized risk never exceeds any
budget), and structural sweeps (_MINT private to the gate; only the submitter
calls broker submit). mypy strict + ruff clean.

Fix during the phase: gate audit rows initially used a random row id, breaking
the `orders.proposal_id -> trade_proposals(id)` FK at submission — rows are
now keyed by the proposal's own id with ON CONFLICT upsert (latest decision).

## Phase G — Deterministic position management (2026-07-22)

Delivered: `src/positions/` — `monitoring.py` (validated PositionMarketState:
mark/spot/Greeks/IV/event/broker flags, Decimal-only, computes unrealized
P&L per net intent); `exit_rules.py` (all five Section 10 dimensions reading
typed ExitPlan keys built by `build_exit_plan`; malformed plan values raise
instead of evaluating to "no exit"; aggregation EXIT > REDUCE > REVIEW >
ALERT > HOLD; `exit_limit_price` slippage-aware closing limits — quarter
spread concession, half under high urgency, never a market order);
`checkpoints.py` (monotonic DTE/assignment escalation ladder NONE ->
DTE_REVIEW -> ASSIGNMENT_WATCH -> FORCED_EXIT -> EMERGENCY; assignment
notice is EMERGENCY at any dte); `emergency.py` (the five Section 10.6
triggers detected from pure state; EmergencyExitEngine records a critical
system event and submits an atomic inverted-leg closing limit order — no LLM
anywhere, sweep-enforced); `degraded.py` (combined view over Phase D
reconciliation `new_entries_allowed` + the kill-switch panel: entries blocked
by everything, exits blocked only by the exit-blocking subset). Extended
`src/execution/submission.py` with the token-free `submit_exit` path:
settlement explicitly never blocks it (`closing_order_cash_check`), and when
the broker lacks the mechanism to reduce risk (e.g. no atomic multi-leg
close) it ALERTS AND HALTS — critical system event + broker_degradation
trip + nothing submitted; legging out is not a code path that exists.

53 new tests (507 total): every dimension's triggers and non-triggers with
exact rules, strict-plan rejection (missing key, float), aggregation
precedence, hand-verified slippage pricing, checkpoint ladder + monotonic
escalation property, all five emergency triggers, the required end-to-end
proofs (emergency exit fires with the model layer entirely absent; degraded
mode rejects a new entry at the gate while a risk-reducing exit walks the
full Section 12.2 path under the same halt; DTE checkpoint fires; missing
exit mechanism halts-and-alerts with zero orders created and subsequent
exits refused). mypy strict + ruff clean.

Note: after Phase F's commit, a repo-wide `ruff format src tests` was found
to have reformatted 12 V1 test files (whitespace only). They were restored
verbatim (`035a48c`); lint/format runs are now scoped to `src tests/v2`
until Phase K archival.

## Phase H — Session orchestration (2026-07-22)

Delivered: `src/orchestration/` — `session.py` (the exact Section 15 state
machine; PREMARKET_RESEARCH reachable only via `complete_startup` with a
passed report; universal DEGRADED/HALTED entry via dedicated methods with
mandatory reasons; recovery edges explicit: DEGRADED resumes only when the
caller proves the condition cleared and only into safe no-new-entry targets,
HALTED leaves ONLY via `manual_resume` with an identified human per
REQUIRE_MANUAL_RESUME_AFTER_HALT — `transition()` refuses both; every change
journaled to system_events); `startup.py` (all ten Section 15.1 checks run
every time, one failure blocks: calendar/session, DB-clock skew, migrations +
14 tables, broker auth, capability-snapshot refresh, reconciliation with
broker positions/open orders captured for restart recovery, market-data
freshness judged at probe time, active-config SHA-256 integrity via
`config_integrity.py` evidence stamping, visible paper/live banner that FAILS
if live orders are ever possible, kill-switch trip+clear self-test verifying
the epoch); `calendar.py` (2026 NYSE holidays/early closes, DST-aware
session phases in UTC, uncovered years raise); `health.py` (reusable probes
that report their own failures instead of raising); `events.py` (synchronous
bus: handler failures isolated + recorded critical, events journaled);
`scheduler.py` (Section 15.2: hard-risk 30s ahead of research 300s enforced
at construction, priority-ordered `run_next`, LLM agent work gated entirely
behind queued meaningful triggers — never due on a tick).

63 new tests (570 total): full-day chain walk with journal assertions,
DEGRADED/HALTED from every state, all recovery-edge refusals, startup
fail-closed proofs (dead broker, missing/tampered config hash, unclean
reconciliation, stale feed), restart-with-open-positions recovery (broker
truth converged, resume into POSITION_MANAGEMENT), one-task-per-tick
starvation simulation (hard-risk gaps bounded at 2x cadence), 1000 idle
ticks -> zero agent invocations, calendar/DST/early-close hand checks,
event-bus failure isolation. mypy strict + ruff clean.

Failures encountered and fixed:

1. Calendar's weekday short-circuit skipped the coverage check on weekends
   (an uncovered year returned False instead of raising) — coverage now
   checked first.
2. `check_market_data` judged freshness against the run-start clock, so a
   quote observed microseconds after run start read as clock skew — probes
   are now judged at the moment they return.

## Phase I — Learning, calibration, and audit tamper evidence (2026-07-22)

Delivered (fully implemented, nothing stubbed): `src/learning/` —
`records.py` (validated TradeRecord with every dimension/metric input;
FillAttempt for fill metrics); `buckets.py` (all thirteen Section 13.2
dimensions with fixed documented band edges; dollar exposures normalized by
the trade's own max risk); `metrics.py` (all Section 13.3 metrics in pure
Decimal — win rate, avg win/loss, expectancy after costs, profit factor,
MAE/MFE, slippage vs. midpoint, fill rate/time-to-fill, return on max risk
and per theta/delta/gamma dollar, Brier score, drawdown + recovery duration;
empty samples refuse, zero-denominator ratios are None never fabricated);
`calibration.py` (per-dimension runs persisted to calibration_results;
MIN_SAMPLE_SIZE=30 hard floor — under-sampled buckets are measured but never
become Auditor evidence); `shadow.py` (ShadowEvaluator re-scores the same
candidates under shadow weights; decisions are inert data persisted to
calibration_results; the learning package has no import path to gate/
execution/brokers — sweep-enforced); `promotion.py` (the full Section 13.4
lifecycle: propose_change re-gates sample size + guardrails + clamp bounds
then creates an integrity-hashed immutable shadow version;
compare_to_control with minimum sample/window and |diff| > 2*SE uncertainty
gate; promote demands an eligible favorable comparison AND a named human;
reject/rollback both journaled; rollback refuses to leave zero actives).

Audit finding 3: migration m0015 hash-chains order_events, agent_decisions,
and system_events — a BEFORE INSERT trigger (covers every writer) stamps
sha256(prev_hash || canonical jsonb) with the timezone GUC pinned so
serialization is stable; `audit_chain_breaks(regclass)` recomputes
server-side; append-only triggers extended to agent_decisions/system_events.
`src/persistence/audit_chain.py` adds HMAC anchors signed with a key held
only in the environment (AUDIT_CHAIN_HMAC_KEY, placeholder in .env.example):
an attacker who rewrites rows and recomputes the whole sha256 chain still
cannot forge an anchor; truncation and anchored-row drift are detected.

39 new tests (609 total): bucket keys across all 13 dimensions with exact
expected values, hand-computed metrics (Brier 0.23 reference; drawdown
110/recovery-2d case), below-minimum-sample proposals refused with nothing
created, out-of-bounds values refused, guardrails re-refused at the
calibration layer, shadow-vs-control scoring divergence with zero orders
anywhere, shadow-decision-is-not-a-token proof at the submitter, comparison
gates + significance hand case, promotion/rollback round trip with journal
assertions, and the chaos suite (mutated row detected past disabled
triggers, deleted row detected, rewritten-chain-cannot-forge-anchors,
truncation detected, key only from env). mypy strict + ruff clean.

## Phase J — Read-only dashboard (2026-07-22)

Delivered: `src/dashboard/` — all seven Section 18 panels as pure SELECT
queries (`panels.py`): A system/agents (session state, startup report,
kill-switch state reconstructed from the chained event stream, per-agent
last decision + model + prompt version, broker capabilities, data freshness,
paper/live banner), B equity/drawdown (curve, HWM, realized from closed
positions, unrealized from latest position snapshots, settled/unsettled),
C Greeks with dollar limits and headroom plus exposure by underlying/
strategy/expiration (sector honestly marked unavailable — not persisted),
D opportunity board with score decomposition and escaped rejection reasons
and an explicit no-action-controls note, E open positions with exit plans,
thesis state, liquidity deterioration, F orders with midpoint-vs-fill
slippage (entry submissions now record the structure midpoint at submit
time) and reconciliation warnings, G calibration buckets, shadow-vs-control,
config lifecycle. FastAPI app (`app.py`): GET-only routes, docs disabled,
localhost-only `serve()`; WebSocket is server-push — inbound frames beyond
ping/subscribe (including binary, oversized, malformed) are counted, logged,
and dispatched to nothing.

Read-only is structural at four layers: (1) `readonly_db.py` provisions a
SELECT-only PostgreSQL role with an additional role-level read-only
transaction default — raw SQL writes die with insufficient_privilege;
(2) the package imports no execution/gate/agents/learning/analytics code
(sweep-enforced; the tiny credit-strategy set is duplicated and pinned to
the gate's by a sync test); (3) every route is GET (enumerated + 405s
asserted); (4) all free text is HTML-escaped at the edge (Phase E injection
stance extended to rendering); money is stringified, never floated.

18 new tests (627 total): eight raw-SQL write attempts refused by privilege,
all seven panels rendered from a seeded snapshot with exact expected values,
hostile `<script>`/injection text escaped end to end, route enumeration,
the Section 19.2 property (row counts + audit-chain heads identical after
every route, hostile methods, and a ten-frame adversarial WS corpus plus
binary garbage), ignored frames counted and logged, localhost-bind refusal.

## Phase D — Broker capability discovery and adapters (2026-07-22)

Delivered: `src/execution/` — typed `BrokerInterface` (limit orders only by
construction, multi-leg atomic or nothing, mandatory idempotency keys); runtime
capability discovery from the MCP tool listing (fail-closed: unknown tools grant
nothing, bare multi-leg tool name not trusted without a `legs` schema) persisted to
`broker_capability_snapshots` with hashed account ids; fully functional paper broker
(partial fills, limit-price discipline, cancel/expire/reject, idempotent replay,
injectable restricted capabilities); Robinhood MCP adapter over an injected
transport (no transport -> BrokerUnavailable, transport error -> BrokerUnavailable,
live submit -> LiveOrdersDisabled before any call while ALLOW_LIVE_ORDERS=False);
event-sourced order state machine over `orders`/`order_events` (illegal transitions
recorded as RECONCILIATION_REQUIRED, duplicate keys rejected in code AND by the DB
UNIQUE constraint, duplicate broker deliveries are no-ops); reconciliation engine
(broker-ahead converges, impossible states flag + critical system_events, missing/
unknown orders flagged, stale SUBMITTED past 60s blocks new entries).
`docs/BROKER_CAPABILITIES.md` documents discovery semantics and the exact human
setup steps for connecting the real MCP later. 47 new tests incl. hypothesis
property tests (duplicate idempotency keys can never create two orders at either
layer; uncertainty always blocks new entries; transition function matches the
Section 12.2 table exactly). Full suite 292 green; mypy/ruff clean.

Failures encountered and fixed:

1. A test asserted `VALIDATED -> SUBMITTED` directly — the machine correctly
   flagged it illegal (Section 12.2 requires STAGED in between). The test path was
   fixed; the machine was right.
2. A reconciliation test mixed a fixed fake clock with the machine's real
   `submitted_at` stamps, making the stale-submitted check time-of-day dependent.
   Fixed by evaluating staleness against real wall-clock in that test.

Notes (Phase B):

- Migration `0004` and `0008` add append-only/immutability triggers on top of the
  verbatim DDL (`strategy_config_versions`, `order_events`) — additive hardening,
  no dialect changes; both are exercised by attempted-bypass tests.
- Ephemeral test Postgres: `TESTCONTAINERS_RYUK_DISABLED=true` is set in the V2
  conftest (containers are stopped explicitly; Ryuk is unreliable on some Windows
  Docker Desktop setups).
- `PAPER_TRADING=True`, `ALLOW_LIVE_ORDERS=False`, `ORDER_MODE="research_only"`
  unchanged throughout; enforced by `tests/v2/test_risk_policy.py`.
