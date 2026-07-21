# Personal Equities Trading System — Architecture & Build Spec
### 8-Agent Adaptive Trading Firm on Robinhood Agentic Trading (MCP) + Claude Fable 5

**Status:** Design spec for Claude Code
**Account context:** Cash account, starting capital $100–$500, Phase 1 of 4 (see scaling plan)
**Design philosophy:** Aladdin-inspired discipline (centralized risk engine, unified data model, full audit trail, strict separation of idea-generation from risk-control) — not a claim of matching Aladdin's scale or accuracy.

---

## 0. Non-negotiable guardrails (hard-coded, never left to agent judgment)

These live in plain code, not prompts. An LLM should never be the last line of defense on capital-at-risk decisions.

```
HARD_STOP_LOSS_PCT = 0.15–0.20      # per position, enforced by code, not agent discretion
MAX_POSITION_PCT_OF_EQUITY = 0.40   # Phase 1; tightens as account grows (see Phase table)
MAX_CONCURRENT_POSITIONS = 3        # Phase 1
MAX_DAILY_LOSS_PCT = 0.10           # halts all new entries for the day if breached
MAX_DRAWDOWN_HALT_PCT = 0.25        # from account high-water mark; halts trading entirely, requires manual review to resume
MIN_SIGNAL_CONFIDENCE_TO_TRADE = 0.65   # calibrated confidence score, not raw model output
ORDER_APPROVAL_MODE = "manual"      # every order requires your explicit approval in Phase 1;
                                    # the approval surface is Robinhood's native manual-approval
                                    # + push notification, NOT the dashboard (see Section 7.8)

# CASH ACCOUNT (confirmed account type). The PDT day-trade count does NOT apply to cash
# accounts, so there is no three-trades-per-five-sessions cap to enforce. The binding
# constraint instead is T+1 settlement: only settled cash may fund a purchase, and a
# position must never be sold before the funds that bought it have settled (that would be
# a good-faith / free-riding violation). Enforced in code — see Section 1.1.
MAX_DAY_TRADES_PER_5_SESSIONS = None   # not applicable to a cash account
ENFORCE_SETTLED_CASH_ONLY = True       # never commit unsettled proceeds to a new purchase
```

Any code change to these values should be a deliberate, logged, human action — not something an agent is ever permitted to modify, even the learning/adaptation agent (Agent 8). Agent 8 can *recommend* a change with evidence; it cannot *apply* one to these hard-coded guardrail values.

---

## 1. Position sizing & stop-loss pseudocode

```python
def calculate_position_size(account_equity, settled_cash, signal, open_positions):
    """
    Returns a target dollar amount to allocate to a new position.
    Fractional shares assumed (Robinhood supports this).
    """
    # 1. Respect settled-cash constraint (T+1 settlement, avoid good-faith violations)
    available_capital = min(settled_cash, account_equity * MAX_POSITION_PCT_OF_EQUITY)

    # 2. Don't exceed max concurrent positions
    if len(open_positions) >= MAX_CONCURRENT_POSITIONS:
        return 0  # no new entries; must wait for an exit

    # 3. Volatility-adjusted sizing (inverse to recent realized volatility, ATR-based)
    atr_pct = signal.atr_14 / signal.current_price
    volatility_scalar = clamp(0.02 / atr_pct, min=0.4, max=1.0)  # dampen size on volatile names

    # 4. Confidence-adjusted sizing
    confidence_scalar = clamp((signal.calibrated_confidence - MIN_SIGNAL_CONFIDENCE_TO_TRADE)
                               / (1.0 - MIN_SIGNAL_CONFIDENCE_TO_TRADE), min=0.3, max=1.0)

    # 5. Sector/correlation check — reduce size if already exposed to correlated names
    correlation_scalar = 1.0
    for pos in open_positions:
        if sector_correlation(pos.symbol, signal.symbol) > 0.6:
            correlation_scalar *= 0.5

    raw_size = available_capital * volatility_scalar * confidence_scalar * correlation_scalar
    return round(min(raw_size, available_capital), 2)


def check_stop_loss(position, current_price):
    loss_pct = (position.entry_price - current_price) / position.entry_price
    if loss_pct >= HARD_STOP_LOSS_PCT:
        return EXIT_SIGNAL("stop_loss", forced=True)   # not overridable by any agent
    return None


def check_portfolio_halt(account_equity, high_water_mark, daily_start_equity):
    drawdown = (high_water_mark - account_equity) / high_water_mark
    daily_loss = (daily_start_equity - account_equity) / daily_start_equity

    if drawdown >= MAX_DRAWDOWN_HALT_PCT:
        return HALT_ALL_TRADING("max_drawdown_breached", requires_manual_resume=True)
    if daily_loss >= MAX_DAILY_LOSS_PCT:
        return HALT_NEW_ENTRIES("daily_loss_limit", resumes_next_session=True)
    return None
```

### 1.1 Settled-cash & good-faith-violation guard (cash account)

This account is a **cash account**, so there is no pattern-day-trader count to manage — the constraint that matters is **T+1 settlement**. When you sell, the proceeds are not settled cash until one business day later. Two violations to design out:

- **Good-faith violation (GFV):** buying a security with unsettled funds, then selling it before the funds that paid for it have settled.
- **Free-riding:** buying and then selling without ever having had settled cash to cover the purchase.

Three GFVs in a rolling 12 months typically restricts the account to settled-cash-only trading for 90 days — an outcome to avoid entirely, not manage reactively.

**The clean way to make both violations structurally impossible: only ever purchase with settled cash.** `calculate_position_size()` already caps `available_capital` at `settled_cash`, so every position is fully paid for with settled funds at the moment of purchase. When that invariant holds, *any* later sale — including a forced stop-loss — can fire freely, because the position was never bought on unsettled money. This also removes the stop-loss-vs-rule tension that a margin account would create: on a cash account with settled-cash-only buying, an exit can never trigger a GFV.

```python
def settled_cash(account, now):
    """Cleared deposits + proceeds from sales whose T+1 settlement date has passed.
       Unsettled proceeds (sold today, not yet settled) are EXCLUDED."""
    return account.cleared_deposits + sum(
        s.proceeds for s in account.recent_sales if s.settlement_date <= now.date()
    )

def assert_purchase_is_covered(order_cost, account, now):
    # Defensive backstop. Sizing already caps buys at settled_cash; this guarantees the
    # invariant even if a bug tried to spend unsettled proceeds. Blocking is correct here:
    # a blocked entry is cheap; a good-faith violation is a 90-day account restriction.
    if not ENFORCE_SETTLED_CASH_ONLY:
        return ALLOW
    if order_cost > settled_cash(account, now) + 1e-6:
        return BLOCK_ENTRY("would_use_unsettled_funds")   # wait for settlement instead
    return ALLOW

def check_stop_loss_cash_account(position, current_price):
    # No settlement guard needed on the SELL side: because the position was bought with
    # settled cash (invariant above), selling it can never create a GFV. Stop-loss is
    # unconditional, exactly as in Section 1.
    return check_stop_loss(position, current_price)
```

Practical consequences to build in:
- **New entries draw only on settled cash.** After a sale, expect roughly a one-business-day wait before those proceeds can fund a new position. The orchestrator should treat unsettled proceeds as unavailable and simply wait, rather than reaching for them — that waiting *is* the GFV avoidance.
- **Optional capital rotation.** If the one-day settlement wait feels constraining at this size, splitting capital into portions that rotate on different days keeps some settled cash available most sessions. This is a strategy choice, not a requirement — the settled-cash cap protects you either way.
- **Surface it on the dashboard.** Replace any day-trade tile with a read-only **settled vs. unsettled cash** tile (e.g. "settled: \$220 available · unsettled: \$140 settling tomorrow"), so you can see at a glance how much capital is actually deployable right now.

---

## 2. System architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        ORCHESTRATOR (Portfolio Manager)               │
│              runs on a schedule (e.g. pre-market + midday + close)    │
│                     Claude Fable 5 — long-horizon reasoning           │
└───────────────┬─────────────────────────────────────────────────────┘
                │
   ┌────────────┼────────────────────────────────────────────────┐
   │            │                                                 │
┌──▼───┐   ┌───▼────┐   ┌──────────┐   ┌──────────┐   ┌─────────▼────┐
│ Ag 1 │   │ Ag 2   │   │  Ag 3    │   │  Ag 4    │   │  Ag 5        │
│Scanner│   │Research│   │ Edge/    │   │Portfolio │   │Risk Manager  │
│      │   │(4 sub- │   │Signal    │   │Construct.│   │(code-first,  │
│      │   │agents) │   │Aggregator│   │Agent     │   │thin LLM layer)│
└──────┘   └────────┘   └──────────┘   └──────────┘   └───────┬──────┘
                                                                │
                                          ┌─────────────────────▼───┐
                                          │  Ag 6 Execution Agent    │
                                          │  (Robinhood MCP client)  │
                                          └─────────────┬────────────┘
                                                          │
                                          ┌─────────────▼────────────┐
                                          │  Ag 7 Position Monitor /  │
                                          │  Exit Agent (continuous)  │
                                          └─────────────┬────────────┘
                                                          │
                                          ┌─────────────▼────────────┐
                                          │  Ag 8 Performance &       │
                                          │  Calibration Auditor      │
                                          │  (the learning loop)      │
                                          └────────────────────────────┘

Persistence: Postgres/SQLite — trade_journal, signal_history, calibration_buckets,
             strategy_config_versions, shadow_test_results
```

**Model allocation — uniform Fable 5 build (single-run construction):**
| Agent | Model | Why |
|---|---|---|
| 1. Scanner | Fable 5 | Uniform model for build simplicity; can be downgraded later per Section 3.7 |
| 2. Research (4 sub-agents) | Fable 5 | Deep reasoning, document/chart vision, long-horizon research |
| 3. Edge/Signal Aggregator | Fable 5 | Uniform model for build simplicity |
| 4. Portfolio Construction | Fable 5 | Uniform model for build simplicity |
| 5. Risk Manager | Code-first; Fable 5 only for narrative flagging | Capital-at-risk logic must not depend on LLM judgment, regardless of model tier |
| 6. Execution | Code-first (MCP client) | No reasoning needed, pure API calls |
| 7. Exit Monitor | Fable 5 | Uniform model for build simplicity |
| 8. Auditor/Learning | Fable 5 | Pattern-finding across large trade history, the hardest reasoning task in the system |

**Note on this choice:** running every LLM-backed agent on Fable 5 is the simplest path to a working single build and is fine for Phase 1's low trade volume, but it is not the lowest-cost or necessarily the most accurate long-term configuration — Section 3.7 explains why, and keeps every agent swappable via one config value once Agent 8 has enough calibration data to show where a different tier helps or is wasted spend.

---

## 3. The learning loop — how agents actually improve from losses (Agent 8, in depth)

This is the part most builds get wrong, so it gets its own section. The core principle: **a loss is not automatically a mistake.** A well-calibrated 65%-confidence trade loses 35% of the time by design. Reacting to individual losses causes overfitting to noise. The system must learn from *statistically significant calibration drift*, not from individual outcomes.

### 3.1 What gets logged on every trade (trade_journal table)
- Entry: symbol, timestamp, entry price, position size, which research sub-agents contributed, each sub-agent's raw signal + confidence, the aggregated calibrated confidence, sector/correlation context, account state at entry
- Market conditions at entry: realized volatility (ATR), volume vs. average, broad market regime (e.g., VIX level/trend)
- Exit: timestamp, exit price, exit reason (stop_loss / take_profit / thesis_invalidated / scheduled_review), realized P&L, holding period

### 3.2 Calibration tracking (calibration_buckets table)
For every closed trade, bucket it by which research agent(s) drove the signal and by confidence band (e.g., 0.65–0.70, 0.70–0.80, 0.80+). Track, per bucket, per rolling window (e.g., trailing 30 / 90 trades):
- Hit rate (% profitable) vs. expected hit rate implied by stated confidence
- Average realized return vs. average predicted return
- Sample size (this gates everything below)

```python
MIN_SAMPLE_SIZE_FOR_ADAPTATION = 30   # do not adjust anything below this

def evaluate_calibration(bucket):
    if bucket.sample_size < MIN_SAMPLE_SIZE_FOR_ADAPTATION:
        return NO_ACTION("insufficient_sample")

    expected_hit_rate = bucket.confidence_midpoint
    observed_hit_rate = bucket.wins / bucket.sample_size
    z_score = statistical_significance_test(observed_hit_rate, expected_hit_rate, bucket.sample_size)

    if abs(z_score) < 1.96:  # not statistically significant at ~95%
        return NO_ACTION("within_normal_variance")

    if observed_hit_rate < expected_hit_rate:
        return PROPOSE_ADJUSTMENT(bucket.source_agent, "reduce_confidence_or_weight", z_score)
    else:
        return PROPOSE_ADJUSTMENT(bucket.source_agent, "increase_weight", z_score)
```

### 3.3 What actually gets adapted, and how
| Layer | What it learns | Mechanism |
|---|---|---|
| Research agents (2) | Systematic overconfidence/underconfidence per signal type, per sector | Confidence recalibration factor applied to raw outputs (e.g., "technical agent's 80% calls resolve at 65% historically → apply -15pt correction") |
| Edge Aggregator (3) | Which research agents are actually adding value vs. noise | Adjust ensemble weighting — agents with poor calibration get down-weighted, not removed outright (avoid overfitting to a bad stretch) |
| Portfolio Construction (4) | Whether volatility/confidence scalars are producing the risk-adjusted returns expected | Adjust `volatility_scalar` and `confidence_scalar` bounds within pre-approved ranges |
| Risk Manager (5) | Whether stop-loss distance and position caps match realized volatility patterns | Can *propose* tighter/looser stops per volatility regime; cannot change the hard-coded guardrails in Section 0 |
| Exit Agent (7) | Whether take-profit/stop timing is leaving money on the table or exiting too early | Adjust rule thresholds based on realized vs. optimal exit analysis (backward-looking, MAE/MFE analysis) |

### 3.4 Regime detection — separate from mistake-correction
Track rolling market-wide volatility/volume metrics independent of your own trade outcomes. If the regime shifts (e.g., VIX spikes, broad volume dries up), widen risk parameters automatically — this isn't "learning from a mistake," it's adapting to conditions, and should never be confused with the calibration-drift logic above.

### 3.5 Shadow-testing before any change goes live
No proposed adjustment from Agent 8 applies to live trading directly:

1. Agent 8 proposes a change with its statistical evidence (z-score, sample size, affected bucket)
2. Change is applied in a **shadow config** — runs in parallel against live signals, paper-only, for a minimum window (e.g., 20 new trades or 30 days, whichever is longer)
3. Shadow performance is compared against the live (control) config
4. Only if shadow outperforms control with its own statistical significance does the change get promoted
5. Every config version is stored (`strategy_config_versions` table) with full history — you can always answer "what parameters were live when this trade happened" and roll back instantly

```python
def promote_shadow_config(shadow_results, control_results):
    if shadow_results.sample_size < MIN_SAMPLE_SIZE_FOR_ADAPTATION:
        return HOLD("shadow_still_accumulating_data")
    if shadow_results.sharpe > control_results.sharpe and \
       statistically_significant(shadow_results, control_results):
        return PROMOTE_TO_LIVE(requires_human_confirmation=True)  # Phase 1: always confirm
    return HOLD("no_significant_improvement")
```

### 3.6 Human checkpoint
In Phase 1, **every** promoted config change surfaces to you for a yes/no confirmation before going live — small parameter nudges included. This is the equivalent of a model-validation sign-off at a real trading firm. Auto-promotion of minor parameter nudges (not structural changes) is a reasonable thing to unlock once you've built trust in the shadow-testing process over several cycles — not before.

---

## 3.7 Model upgrade path — build model-agnostic from day one

The model assignments in Section 2's table are a starting allocation, not a fixed architecture. Claude Code should build every agent's model call behind a single config-driven interface so swapping models later is a one-line change, not a rewrite.

```python
# config/models.py — single source of truth, not hardcoded per-agent
# Uniform Fable 5 build for Phase 1 (simplest path to one working build).
# Every value below is independently swappable later with zero changes to agent logic.
AGENT_MODELS = {
    "scanner":              "claude-fable-5",
    "research_technical":   "claude-fable-5",
    "research_fundamental": "claude-fable-5",
    "research_sentiment":   "claude-fable-5",
    "research_macro":       "claude-fable-5",
    "edge_aggregator":      "claude-fable-5",
    "portfolio_construct":  "claude-fable-5",
    "risk_manager_flagging":"claude-fable-5",   # narrative flagging only; core logic is pure code
    "exit_monitor":         "claude-fable-5",
    "auditor_calibration":  "claude-fable-5",
}
```

Every agent function should read its model from this config rather than hardcoding a model string, so any single row can be changed without touching agent logic.

**Where a different tier fits as an upgrade/downgrade slot, once the data justifies it:**

| Slot | Trigger to consider the swap | Direction |
|---|---|---|
| `edge_aggregator` | Agent 8's calibration tracking shows the aggregator is well-calibrated on a task that doesn't need frontier reasoning | Downgrade to Sonnet 5 — save cost with no accuracy loss |
| `exit_monitor` | Thesis-invalidation calls (deciding whether new information genuinely reverses a position) show strong, consistent hit-rate on straightforward rule-following | Downgrade to Sonnet 5 |
| `scanner` | High call volume, simple filtering logic, no observed accuracy gain from frontier reasoning | Downgrade to Haiku 4.5 — this is the highest-volume agent and the biggest cost-saving opportunity |
| `auditor_calibration` (as a cross-check, not a replacement) | Once trade volume is high enough that a second opinion on proposed calibration changes is worth the added cost | Add Opus 4.8 as an independent reviewer of Fable 5's proposed adjustments before they enter shadow-testing — disagreement between the two is itself a useful signal to slow down |

**Decision rule for any model swap:** don't change tiers on intuition — change when Agent 8's own calibration data shows a specific agent's outputs justify it (either poor hit-rate relative to stated confidence, arguing for more capability, or consistently strong performance on a simple task, arguing a cheaper model would do just as well). Validate any swap through the shadow-testing framework in Section 3.5 before promoting it to live. This keeps model selection itself evidence-driven rather than guessed, consistent with how the rest of the system is meant to operate.

## 3.8 Model availability resilience — designing for the Fable 5 suspension precedent

Fable 5 was fully suspended for US export-control reasons for nearly three weeks in June 2026, with the suspension taking effect within 90 minutes of notice. That's a real precedent, not a hypothetical: a live trading system with open positions cannot assume its primary reasoning model will always be reachable. This is separate from any billing/plan changes (e.g., the July 7 shift from included usage to metered credits on consumer plans) — API access is pay-as-you-go and isn't affected by that change. The suspension risk is about government-directed access removal, which has happened once already and could recur.

**Design requirement: no agent whose failure could strand an open position is allowed to be a single point of failure.**

- **Risk Manager (5) and stop-loss/take-profit checks (Section 1) are already pure code** — this is the most important resilience property in the whole system. Even a full Fable 5 outage cannot prevent a stop-loss from firing, because it was never implemented as a model call in the first place.
- **Add automatic failover to the `AGENT_MODELS` config**, not just manual swap capability:

```python
# config/models.py
AGENT_MODELS = {
    "research_technical":   "claude-fable-5",
    # ... etc, all default to claude-fable-5 per the single-run build
}

FAILOVER_CHAIN = {
    "claude-fable-5": "claude-opus-4-8",   # if Fable 5 is unreachable, fall back one tier
    "claude-opus-4-8": "claude-sonnet-5",  # further fallback if needed
}

MAX_MODEL_UNAVAILABLE_RETRIES = 3
```

- Every agent call should be wrapped so that a sustained API failure (not a transient error — distinguish the two) triggers automatic failover to the next model in the chain, logs the failover event, and flags it in the audit trail. A trade decided under a failover model should be tagged as such in `trade_journal`, so Agent 8's calibration tracking can separately evaluate whether failover-model decisions perform differently from primary-model decisions.
- **Exit Monitor (7) should have the shortest failover tolerance of any agent** — if it can't reach any model in the chain within a defined timeout, the system should default to the hard-coded stop-loss/take-profit rules in Section 1 alone (no LLM judgment needed for a forced exit) rather than leaving a position unmonitored.
- **New order entries should be more conservative under failover** than exits — consider disabling new position entries entirely (not just switching models) if the primary research/edge agents are running on a fallback model, until Fable 5 access is confirmed restored. Existing positions still get managed; new capital doesn't get committed on a degraded reasoning stack.

This doesn't require rebuilding anything — it's the same config-driven interface from Section 3.7, extended with automatic (not just manual) switching and a policy for what the system is and isn't allowed to do while running on a fallback model.

---

## 4. Database schema (core tables)

```sql
CREATE TABLE trade_journal (
    trade_id UUID PRIMARY KEY,
    symbol TEXT,
    entry_ts TIMESTAMPTZ,
    entry_price NUMERIC,
    position_size_usd NUMERIC,
    contributing_agents JSONB,        -- {agent_name: {raw_signal, confidence}}
    aggregated_confidence NUMERIC,
    account_equity_at_entry NUMERIC,
    atr_pct_at_entry NUMERIC,
    market_regime_at_entry TEXT,
    exit_ts TIMESTAMPTZ,
    exit_price NUMERIC,
    exit_reason TEXT,
    realized_pnl NUMERIC,
    holding_period_hours NUMERIC,
    config_version_id UUID REFERENCES strategy_config_versions(id)
);

CREATE TABLE calibration_buckets (
    bucket_id UUID PRIMARY KEY,
    source_agent TEXT,
    confidence_band TEXT,             -- e.g. '0.65-0.70'
    window_start TIMESTAMPTZ,
    window_end TIMESTAMPTZ,
    sample_size INT,
    wins INT,
    observed_hit_rate NUMERIC,
    expected_hit_rate NUMERIC,
    z_score NUMERIC
);

CREATE TABLE strategy_config_versions (
    id UUID PRIMARY KEY,
    created_ts TIMESTAMPTZ,
    parameters JSONB,                 -- full snapshot of all tunable params
    promoted_by TEXT,                 -- 'human_confirmed' always in Phase 1
    proposed_by_agent TEXT,
    evidence JSONB                    -- z-scores, sample sizes that justified the change
);

CREATE TABLE shadow_test_results (
    id UUID PRIMARY KEY,
    config_version_id UUID REFERENCES strategy_config_versions(id),
    start_ts TIMESTAMPTZ,
    end_ts TIMESTAMPTZ,
    trades_count INT,
    sharpe_ratio NUMERIC,
    hit_rate NUMERIC,
    promoted BOOLEAN
);

-- Time-series account value, written on every orchestrator/monitor tick.
-- This is the data source for the live "stock chart" of total account balance (Section 7).
CREATE TABLE equity_snapshots (
    snapshot_id UUID PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL,
    total_equity NUMERIC,             -- cash + marked-to-market open positions
    settled_cash NUMERIC,
    open_positions_value NUMERIC,
    high_water_mark NUMERIC,
    open_position_count INT,
    is_paper BOOLEAN,                 -- true while PAPER_TRADING = True
    source TEXT                       -- 'monitor_tick' / 'post_trade' / 'scheduled_close'
);
CREATE INDEX idx_equity_snapshots_ts ON equity_snapshots (ts);

-- One row per calendar trading day, the source for the calendar heatmap and the
-- daily/weekly/monthly/yearly P&L breakdown (Section 7). Rolled up at session close
-- from trade_journal (realized) + equity_snapshots (unrealized/mark-to-market).
CREATE TABLE daily_pnl (
    trading_date DATE PRIMARY KEY,
    starting_equity NUMERIC,
    ending_equity NUMERIC,
    realized_pnl NUMERIC,             -- from trades closed that day
    unrealized_pnl_change NUMERIC,    -- mark-to-market move on still-open positions
    total_pnl NUMERIC,                -- realized + unrealized change
    total_pnl_pct NUMERIC,
    trades_opened INT,
    trades_closed INT,
    wins INT,
    losses INT,
    is_paper BOOLEAN
);
```

---

## 5. Phased scaling plan (risk parameters by phase)

| Phase | Equity range | Max positions | Max position % | Approval mode |
|---|---|---|---|---|
| 1 | $100–$1,000 | 1–3 | 40% | Manual, every order |
| 2 | $1,000–$5,000 | 4–6 | 25% | Manual below threshold confidence, auto above |
| 3 | $5,000–$25,000 | 6–10 | 15% | Auto with daily review |
| 4 | $25,000+ | 10+ | 10% | Auto with circuit breakers only |

Graduation between phases should be a manual decision you make after reviewing Agent 8's cumulative calibration report — not an automatic trigger.

---

## 6. Claude Code build instructions — single-run build, uniform Fable 5

Hand this whole spec file to Claude Code and have it build the entire system in one session, using `claude-fable-5` for every LLM-backed agent per the config in Section 3.7. The sequencing below is the internal order Claude Code should build components in *within that one run* — it's about dependency order (you can't wire execution to a risk manager that doesn't exist yet), not separate sittings. Nothing here requires stopping and waiting between steps.

**Build directive for Claude Code:**

> Build the full 8-agent trading system described in this spec in one pass. Use `claude-fable-5` as the model for every LLM-backed agent call, wired through the `config/models.py` interface in Section 3.7 — do not hardcode model strings anywhere else. Build in this internal dependency order:
>
> 1. **Data & persistence layer** — the Postgres/SQLite schema (Section 4), plus a market-data ingestion module (equities OHLCV, ATR, volume — yfinance or Alpaca market data is sufficient for Phase 1).
> 2. **Research agents + Edge Aggregator (Agents 2–3)** — the 4 research sub-agents and the aggregator, all as `claude-fable-5` calls per the config, each returning structured JSON (direction, magnitude, confidence, reasoning summary). Every output must write to `trade_journal`/`calibration_buckets`-adjacent tables regardless of whether a trade is placed.
> 3. **Risk & sizing (Agents 4–5) as pure code** — implement Section 1's pseudocode exactly, in code, never as an LLM prompt. This includes the Section 1.1 settled-cash / good-faith-violation guard: purchases draw only on settled cash (`ENFORCE_SETTLED_CASH_ONLY = True`), which makes GFV and free-riding structurally impossible on this cash account. Write unit tests asserting the system cannot violate the Section 0 hard guardrails — including that no purchase can ever be funded with unsettled proceeds — before writing anything that could violate them.
> 4. **Execution (Agent 6) via Robinhood MCP** — integrate against Robinhood's Agentic Trading MCP server (agent.robinhood.com/mcp/trading; docs at robinhood.com/us/en/support/agentic-trading). Default `ORDER_APPROVAL_MODE = "manual"` in config — every order must be approved before it executes. Since Agent 6 is headless code (no interactive MCP-client prompt to catch), configure **manual approval on the Robinhood agentic account itself** so the human confirm/reject happens via Robinhood's native trade-preview + push notification (Section 7.8), not in this system's own UI. The backend stages and submits the order; Robinhood is where you approve it. Confirm the dedicated Agentic Account isolation before any live funding.
> 5. **Exit monitoring (Agent 7)** — stop-loss/take-profit as pure code (Section 1) on a poll loop; thesis-invalidation as a `claude-fable-5` call per config.
> 6. **Auditor & learning loop (Agent 8)** — full calibration bucket tracking and shadow-testing framework from Section 3, using `claude-fable-5`. This is the component that must not be simplified or stubbed — build it completely, since it's what makes the rest of the system self-correcting rather than a static script.
> 7. **Wire the orchestrator** to run the full pipeline end-to-end in paper mode by default (`ORDER_APPROVAL_MODE = "manual"` plus a `PAPER_TRADING = True` flag) so the whole system is testable in one run without placing a single real order until that flag is deliberately flipped.
> 8. **GUI / real-time dashboard (Section 7)** — build the monitoring dashboard last, once the data and event streams it reads from exist. It is a **strictly read-only** observation surface over the backend built in steps 1–7; it must never contain its own copy of trading, sizing, or guardrail logic, and it must have no path that places, approves, resizes, or cancels an order. All actual order placement and any order approval happen on Robinhood (via the Agentic Trading MCP flow / Robinhood's own interface), never through this dashboard. The GUI only watches.
>
> Throughout: every agent call and every decision must log enough context to fully reconstruct "why did the system do this" after the fact. Keep risk/execution logic free of LLM calls wherever Section 1's deterministic rules apply — even though every agent is Fable 5, reserve actual model calls for genuine reasoning tasks (research synthesis, thesis checks, calibration pattern-finding), not arithmetic or guardrail enforcement.

**After the build completes:** run the system with `PAPER_TRADING = True` until at least one calibration bucket clears `MIN_SAMPLE_SIZE_FOR_ADAPTATION` (Section 3.2) before funding the account and flipping to live orders. That validation step doesn't disappear just because the build itself happens in one run.

**Cross-cutting instructions for Claude Code on every milestone:**
- Write tests for the guardrails in Section 0 before writing the feature that could violate them
- Every agent call and every decision must be logged with enough context to reconstruct "why did the system do this" after the fact
- Keep the risk/execution code path free of LLM calls wherever a deterministic rule suffices — reserve Fable 5 for genuine reasoning tasks (research synthesis, calibration pattern-finding), not arithmetic

---

## 7. GUI / real-time dashboard — build instructions for Claude Code

The dashboard is a **strictly read-only observation surface** over the backend built in Sections 1–6. It renders what the system is doing; it does not decide anything and it does not place, approve, resize, or cancel orders. All trading, sizing, and guardrail logic stays in the backend (Sections 0–1) exactly as specified, and **all actual order placement and any order approval happen on Robinhood** — via the Agentic Trading MCP flow and Robinhood's own interface — not in this dashboard. The GUI only watches.

This separation is deliberate and mirrors the same idea-generation-vs-risk-control split as the rest of the spec: a rendering layer must never become a second, undisciplined path to placing orders. Keeping the dashboard entirely read-only means there is simply no way for it to move real money, no matter what happens to it.

### 7.0 Recommended stack

- **Backend API:** FastAPI (Python) alongside the existing system, so the dashboard reads the same Postgres/SQLite tables and in-process state the agents write to — no separate data copy.
- **Real-time transport:** WebSocket (or Server-Sent Events if simpler) for live agent status and price/equity ticks; plain REST for historical queries (calendar, P&L rollups, chart history).
- **Frontend:** React single-page app. Charting via a candlestick-capable library (Lightweight Charts by TradingView, or Recharts for the simpler line/area views). A calendar-heatmap component for the trading calendar.
- **Auth:** the dashboard binds to localhost by default and is single-user. Do not expose it to the network without adding authentication first — even though it's read-only, it displays live account balances and positions, which you don't want served to anyone else.

### 7.1 Panel A — live agent activity board (all 8 agents in real time)

A always-visible board showing every agent (Orchestrator + Agents 1–8) as a card with:
- **Current state:** `idle` / `running` / `waiting_on_upstream` / `blocked` / `error` / `failover` (the last one surfaces the Section 3.8 failover state — show which model each agent is currently running on, and highlight any agent not on its primary Fable 5).
- **Last action + timestamp,** and for the LLM-backed agents, a one-line summary of their most recent output (e.g. Research-Technical: "AAPL long, conf 0.72").
- **Live signal flow:** a small pipeline visualization showing a signal moving Scanner → Research → Edge Aggregator → Portfolio Construction → Risk Manager → Execution, so you can watch a candidate trade progress and see exactly where it gets sized down or rejected.
- **Guardrail status strip:** live readouts of the Section 0 values — current open positions vs. `MAX_CONCURRENT_POSITIONS`, daily loss vs. `MAX_DAILY_LOSS_PCT`, drawdown vs. `MAX_DRAWDOWN_HALT_PCT`. If a halt fires, this strip turns red and states which limit tripped. This is display only; the halt itself is enforced in code regardless of the GUI.

Implementation: agents publish a status event (`agent_name`, `state`, `summary`, `active_model`, `ts`) to a lightweight in-process event bus on every state change; the backend relays those over the WebSocket. Persist the last event per agent so a freshly opened dashboard shows current state immediately, not a blank board.

### 7.2 Panel B — live account balance chart (stock-chart style)

A chart that looks and behaves like a stock chart, but the "instrument" is your **total account equity**.
- **Source:** the `equity_snapshots` table (Section 4), written on every monitor/orchestrator tick, plus a live-appended point pushed over the WebSocket on each new snapshot.
- **Rendering:** area/line chart for the balance line; candlesticks optional if you aggregate snapshots into per-interval OHLC (e.g. 5-min candles of equity). Overlay the **high-water mark** line (drives the drawdown guardrail) and shade the current drawdown band beneath it.
- **Timeframe selector:** 1D / 1W / 1M / 3M / YTD / All, each querying the appropriate `equity_snapshots` range and downsampling for longer windows.
- **Paper vs. live:** while `PAPER_TRADING = True`, badge the chart clearly as paper equity so paper and live curves are never visually confused. Filter by the `is_paper` column so the two are never mixed on one line.

### 7.3 Panel C — trading calendar (daily P&L)

A month-grid calendar heatmap, one cell per trading day, colored by that day's P&L (green gains / red losses, intensity scaled to magnitude).
- **Source:** the `daily_pnl` table (Section 4), rolled up at each session close.
- **Cell contents:** date, total P&L ($ and %), and trades closed that day. Hover/tap opens a day detail: every trade opened or closed that day pulled from `trade_journal`, with entry/exit/reason/realized P&L.
- **Month/year navigation,** plus a running month-to-date total in the header.
- Empty (non-trading) days render neutral, not as zero-P&L green/red, so weekends and holidays don't distort the visual.

### 7.4 Panel D — P&L breakdown (daily / weekly / monthly / yearly)

A summary panel with four columns or tabs — **Today, This Week, This Month, This Year** — each showing:
- Total P&L ($ and %), realized vs. unrealized split, number of trades, win rate, and best/worst trade for the period.
- A period-over-period delta where meaningful (e.g. this week vs. last week).
- A small sparkline of the equity curve for that period.

All figures are aggregations over `daily_pnl` (and `trade_journal` for the trade-level stats), computed in the backend via date-bucketed SUM queries — daily = today's row, weekly = current ISO week, monthly = current calendar month, yearly = current calendar year. Compute these server-side and return them as JSON; don't recompute rollups in the browser. Respect the `is_paper` flag so the breakdown matches whichever mode the account is in.

### 7.5 Backend endpoints (minimum set — all read-only)

Every endpoint is a GET or a read-only WebSocket stream. There is no POST, PUT, PATCH, or DELETE. The dashboard has no endpoint that can place, approve, resize, or cancel an order — that all lives on Robinhood.

```
GET  /api/agents/status         -> current state of all agents (also streamed via WS)
WS   /ws/agents                 -> live agent status + signal-flow events
GET  /api/equity?range=1D|1W|1M|3M|YTD|ALL   -> equity_snapshots series for the chart
WS   /ws/equity                 -> live equity tick appends
GET  /api/calendar?month=YYYY-MM             -> daily_pnl rows for the calendar
GET  /api/day/{YYYY-MM-DD}      -> trade detail for one day
GET  /api/pnl/summary           -> daily/weekly/monthly/yearly breakdown blocks
GET  /api/positions/open        -> current open positions, marked to market
GET  /api/orders/recent         -> recently staged/placed orders, DISPLAY ONLY (mirrors
                                   what the backend sent to Robinhood; the dashboard never
                                   originates or confirms an order — that happens on Robinhood)
```

### 7.6 Guardrails specific to the GUI

- **Read-only by construction.** Every endpoint is a read; there are no state-mutating endpoints at all. The dashboard has no code path that reaches the Robinhood MCP client, stages an order, or confirms one. Order placement and approval happen entirely on Robinhood. If the whole dashboard process were compromised or crashed, it could not move a single dollar, because it was never wired to.
- **The dashboard cannot edit Section 0 values.** It may *display* the guardrails and *display* Agent 8's proposed changes awaiting the Section 3.6 human checkpoint, but the confirm action for a config change lives in the backend's existing human-checkpoint flow, logged to `strategy_config_versions` — not as a toggle in the UI.
- **Paper/live is unmistakable.** A persistent global banner states whether `PAPER_TRADING` is True or False and whether any agent is on a failover model (Section 3.8). Live mode should be visually distinct enough that it's never mistaken for paper.
- **Fail visible, not silent.** If the WebSocket drops or a data source is stale, the affected panel shows a clear "stale / disconnected" state rather than displaying last-known numbers as if they were current — misleading a human into thinking a halted or disconnected system is running normally is its own risk.

### 7.7 Build order for the GUI (within the single run)

1. FastAPI read endpoints over the existing tables (`/api/equity`, `/api/calendar`, `/api/pnl/summary`, `/api/agents/status`) — these work as soon as Sections 1–6 are writing data.
2. WebSocket layer for live agent status and equity ticks, fed by the in-process event bus.
3. React shell with the four panels (A: agent board, B: balance chart, C: calendar, D: P&L breakdown).
4. The read-only recent-orders view (`GET /api/orders/recent`) so you can see what the backend sent to Robinhood — display only, no action controls.

Build the dashboard against the paper-mode system first (`PAPER_TRADING = True`), so every panel can be validated on paper data. Because the dashboard is read-only, it is never in the path of a real order regardless of paper/live state — but validating it on paper data still confirms every panel renders correctly before real money is involved.

### 7.8 Where order approval and notifications actually happen (Robinhood, not the dashboard)

Because the dashboard is strictly read-only (Section 7.6) and all trading is done on Robinhood, the approve/reject step and the "you have an order to approve" notification both live on **Robinhood's side**, not in this system. Concretely:

- **Push notifications come from Robinhood.** The Robinhood agentic account sends a push notification for every trade the agent makes, backed by a real-time activity feed and P&L in the Robinhood app. This is your primary real-time alert channel — the custom dashboard mirrors the same data for a fuller view, but the *ping* comes from Robinhood.
- **Approval is a Robinhood setting, configured once.** Set the agentic account to manual-approval mode. In that mode the agent prepares/previews the order and Robinhood sends you a notification with the order details (ticker, side, quantity, estimated price) and a confirm/reject action — you approve or reject inside the Robinhood app. Robinhood also supports a dollar threshold (auto-execute below `$X`, require manual approval above), which maps naturally onto later phases in Section 5. **For Phase 1, keep manual approval on *every* order** — trade volume is low, so per-order approval costs you almost nothing, and it keeps you in the loop while you build trust in the system and consciously manage which settled cash gets deployed. The dollar threshold becomes attractive only in later phases once volume makes per-order approval a friction tax.
- **This system's job is to stage, not to confirm.** The backend runs its Section 0/1 guardrails, decides an order is permissible, and submits it to Robinhood. Robinhood then holds it for your approval and notifies you. Nothing in this codebase — GUI or backend — is the surface where you tap "approve"; keeping that on Robinhood is what makes the read-only dashboard safe by construction.
- **The MCP-client "ask every time" prompt is not your approval path here.** Interactive clients (Claude Desktop/Code) can prompt on each action, but a headless scheduled orchestrator won't reliably surface that prompt to a human. Do not rely on it; rely on Robinhood's account-level manual-approval setting instead.

**Optional secondary notifier (informational only).** If you want a heads-up from your own system in addition to Robinhood's push — e.g., the moment the backend *stages* an order, before Robinhood even notifies you — you can add a one-way outbound notifier (email, SMS, Telegram, etc.) that fires from the backend on staging and on fill. Keep it strictly informational: it reports what happened and links you to the Robinhood app to act. It must never itself be an approve/reject control, because that would recreate exactly the second order-placement path Section 7.6 forbids. Treat it as an alert, not an action surface.

---

## 8. Claude Code execution directive — autonomous one-run build with continuous self-validation

Sections 0–7 define *what* to build. This section defines *how Claude Code should drive the build*: in a single continuous session, on `claude-fable-5`, validating and debugging its own work at every step, and holding the finished system to the spec exactly — no stubs, no skipped components, no weakened guardrails. Hand Claude Code this entire spec file plus the directive below.

> **Master build directive for Claude Code**
>
> Build the complete 8-agent **cash-account** trading system defined in Sections 0–7 in one continuous run. Use `claude-fable-5` for every LLM-backed agent, wired through the `config/models.py` interface (Section 3.7) — never hardcode a model string elsewhere. Build in the dependency order given in Section 6 (data/persistence → research + aggregator → risk/sizing as pure code → execution via Robinhood MCP → exit monitoring → Agent 8 learning loop → orchestrator → GUI). Do not stop and wait for me between components; work straight through to a fully paper-runnable system.
>
> **Set up the environment first.** Create a virtualenv, pin dependencies in a lockfile, and configure a linter (ruff/flake8) and a type checker (mypy/pyright) before writing feature code, so validation is available from the first component onward.
>
> **Operate in a continuous validate-and-repair loop.** For every component, before you consider it done:
> 1. Write its tests first or alongside it (test-driven where practical), then run them.
> 2. Run the linter and type checker on what you just wrote; resolve every error and warning.
> 3. Actually execute the component/module to confirm it *runs*, not merely that it imports.
> 4. If anything fails — test, lint, type, or runtime — diagnose the root cause yourself, fix it, and re-run. Repeat until green. Never advance to the next component with a known failure behind you.
> 5. Append to a running `BUILD_LOG.md`: what you built, what failed, the root cause, and the fix — so the entire build is auditable after the fact.
>
> **If you get stuck on one component,** isolate the failure, record it clearly in `BUILD_LOG.md` with your best diagnosis, and continue building the independent components around it — do not halt the whole build, and do not fake a pass to move on. A clearly-flagged unresolved item is acceptable; a silently broken or faked one is not.
>
> **NEVER weaken a guardrail or the spec to make a step pass — this is the single most important rule of the entire build.** If a test asserting a Section 0 hard limit or the Section 1.1 settled-cash invariant fails, the *feature* is wrong, never the guardrail. You are specifically forbidden to: change any value in Section 0; relax `ENFORCE_SETTLED_CASH_ONLY`; remove, soften, or bypass the good-faith-violation guard; delete, skip, comment out, or `xfail` a guardrail test; stub or simplify Agent 8; or replace a real check with a placeholder that always passes. If a guardrail test cannot pass without weakening it, stop and surface the conflict in `BUILD_LOG.md` rather than working around it.
>
> **"Continuously improve" means code quality and spec fidelity — not strategy drift.** You may refactor for clarity, tighten error handling, remove duplication, and improve tests freely, provided every change still satisfies the spec and all guardrail tests. You may **not** invent, tune, or "optimize" trading parameters, confidence thresholds, sizing scalars, or exit rules beyond what the spec states — parameter adaptation is Agent 8's runtime job, gated by shadow-testing and human confirmation (Section 3.5–3.6), and must not happen during the build.
>
> **Do not touch real money at any point in the build.** Keep `PAPER_TRADING = True` and `ORDER_APPROVAL_MODE = "manual"` throughout. Build and test the Robinhood MCP execution path (Agent 6) against its paper/manual-approval flow only; no code path may place, fund, or confirm a live order during the build. Market data for validation comes from the ingestion module (yfinance/Alpaca) or mocks, never from live trading.
>
> **Final spec-fidelity pass.** Once the system runs green end-to-end, re-read each section of this spec and verify the implementation matches it point by point — every agent present and on `claude-fable-5`, every table from Section 4 created, every guardrail from Sections 0 and 1.1 enforced by a passing test, Agent 8 fully built, all four GUI panels (Section 7) reading real data, the whole thing read-only except Robinhood-side approval. Fix any gap you find, then record the result.

### 8.1 Definition of done (all must be true before the build is considered complete)

- [ ] Every component in Section 6's dependency order exists, runs, and is wired end-to-end through the orchestrator.
- [ ] All eight agents make model calls exclusively through `config/models.py`, defaulting to `claude-fable-5`; the Section 3.8 failover chain is wired and unit-tested.
- [ ] Agent 8 (calibration tracking + shadow-testing) is fully implemented — not stubbed, not simplified.
- [ ] The full test suite passes, and it includes explicit guardrail tests proving the Section 0 hard limits **and** the Section 1.1 settled-cash / GFV invariant cannot be violated (each test tries to violate the limit and asserts the system blocks it).
- [ ] Linter and type checker are clean.
- [ ] All four Section 4 core tables plus `equity_snapshots` and `daily_pnl` are created and written to by the relevant components.
- [ ] All four GUI panels (Section 7) render from real backend data; the dashboard has no state-mutating endpoint (read-only except Robinhood-side approval).
- [ ] The system runs end-to-end with `PAPER_TRADING = True` and `ORDER_APPROVAL_MODE = "manual"`; no reachable code path places a live order.
- [ ] No `TODO`s, placeholder returns, `pass`-only stubs, or dead code remain in shipped components.
- [ ] `BUILD_LOG.md` exists and reconstructs the build, and a final report states: what was built, test results, any deviations from the spec with reasons, and the exact command to launch the system in paper mode.

### 8.2 What still requires you, the human, after the build

The one-run build produces a validated, paper-mode system — it does not put real money at risk, by design. Before any live trading, and independent of how clean the build is:

- Run the system in paper mode until at least one calibration bucket clears `MIN_SAMPLE_SIZE_FOR_ADAPTATION` (Section 3.2), per Section 6.
- Confirm the dedicated Robinhood Agentic Account isolation and set the account to manual-approval mode (Section 7.8) before funding.
- Only then deliberately flip `PAPER_TRADING = False`. That flag flip is a human action the build never performs for you.
