# Personal Equities Trading System — 8-Agent Cash-Account Firm (Phase 1, Paper)

An 8-agent adaptive equities trading system built to the accompanying build spec
(`Robinhood_Agentic_Trading_-_Build_Spec_1.md`). It runs **strictly in paper mode**
(`PAPER_TRADING = True`, `ORDER_APPROVAL_MODE = "manual"`); no code path places, funds, or
confirms a live order. Every LLM-backed agent runs on `claude-fable-5`, resolved only through
`config/models.py`.

## Quick start

```powershell
# 1. Create the venv and install pinned deps
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. Run the full 8-agent pipeline end-to-end in paper mode (hermetic: no network/keys)
.\.venv\Scripts\python.exe run_paper.py

# 3. Launch the read-only dashboard (reads the same SQLite tables)
.\.venv\Scripts\python.exe -m uvicorn dashboard.api:app --host 127.0.0.1 --port 8000
#    then open http://127.0.0.1:8000
```

### Validation

```powershell
.\.venv\Scripts\python.exe -m pytest      # full test suite (107 tests)
.\.venv\Scripts\ruff.exe   check .        # linter
.\.venv\Scripts\mypy.exe   .              # type checker
```

## Architecture (Section 2)

```
Orchestrator ─ Scanner(1) ─ Research(2: technical/fundamental/sentiment/macro)
             ─ Edge Aggregator(3) ─ Portfolio Construction(4) ─ Risk Manager(5)
             ─ Execution(6, Robinhood MCP) ─ Exit Monitor(7) ─ Auditor/Learning(8)
Persistence: SQLite  ·  Dashboard: read-only FastAPI + React (CDN)
```

- **Guardrails are pure code**, never an LLM decision: `config/guardrails.py` (Section 0) and
  `risk/sizing.py` (Section 1 & 1.1). Agent 8 may *propose* tunable changes; it can never edit a
  Section 0 guardrail. Cash-account rule: purchases draw only on **settled cash**, making
  good-faith / free-riding violations structurally impossible.
- **Model resilience** (Section 3.8): `config/models.py` defines the `claude-fable-5 → opus-4-8
  → sonnet-5` failover chain; the client fails over on sustained outage and tags the decision.
  New entries are disabled while running on a fallback model; forced stops are pure code and
  keep working even in a full model outage.

## Modes (env vars)

| Var | Default | Meaning |
|---|---|---|
| `TRADING_MARKET_DATA` | `offline` in run_paper/tests | `auto` tries yfinance then falls back; `online` requires it |
| `TRADING_LLM` | `offline` in run_paper/tests | `live` uses real `claude-fable-5` (needs `ANTHROPIC_API_KEY`) |
| `TRADING_DB_PATH` | `data/trading.db` | SQLite path |

## Going live (human-only, after the build — Section 8.2)

1. Run in paper until at least one calibration bucket clears
   `MIN_SAMPLE_SIZE_FOR_ADAPTATION` (30).
2. Confirm a dedicated Robinhood **Agentic Account** and set it to **manual-approval** mode
   (approval + push notifications happen on Robinhood, not in this system).
3. Only then deliberately set `PAPER_TRADING = False`. The build never does this for you.
