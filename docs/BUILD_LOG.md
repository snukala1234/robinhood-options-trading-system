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

Notes:

- Migration `0004` and `0008` add append-only/immutability triggers on top of the
  verbatim DDL (`strategy_config_versions`, `order_events`) — additive hardening,
  no dialect changes; both are exercised by attempted-bypass tests.
- Ephemeral test Postgres: `TESTCONTAINERS_RYUK_DISABLED=true` is set in the V2
  conftest (containers are stopped explicitly; Ryuk is unreliable on some Windows
  Docker Desktop setups).
- `PAPER_TRADING=True`, `ALLOW_LIVE_ORDERS=False`, `ORDER_MODE="research_only"`
  unchanged throughout; enforced by `tests/v2/test_risk_policy.py`.
