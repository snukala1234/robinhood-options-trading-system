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

Notes (Phase B):

- Migration `0004` and `0008` add append-only/immutability triggers on top of the
  verbatim DDL (`strategy_config_versions`, `order_events`) — additive hardening,
  no dialect changes; both are exercised by attempted-bypass tests.
- Ephemeral test Postgres: `TESTCONTAINERS_RYUK_DISABLED=true` is set in the V2
  conftest (containers are stopped explicitly; Ryuk is unreliable on some Windows
  Docker Desktop setups).
- `PAPER_TRADING=True`, `ALLOW_LIVE_ORDERS=False`, `ORDER_MODE="research_only"`
  unchanged throughout; enforced by `tests/v2/test_risk_policy.py`.
