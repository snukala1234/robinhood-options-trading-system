# Private Institutional Options Portfolio Management System - Version 2.0

## Architecture, Risk, Data, Multi-Agent Design, and Claude Code Build Specification

**Status:** Version 2.0 target architecture and implementation specification  
**Deployment:** Private, single-user, single-tenant  
**Broker integration:** Robinhood Agentic Trading through the Trading MCP, subject to the tools and order types actually exposed by the connected account  
**Primary trading specialization:** Short-duration listed equity and ETF options, normally 7-28 calendar days to expiration  
**Initial operating mode:** Paper/research mode, then human approval, then narrowly scoped autonomy only after validation  
**Design philosophy:** Institutional discipline without SaaS complexity: centralized deterministic risk, options-native analytics, complete auditability, independent risk veto, resilient execution, model-agnostic reasoning, and no LLM as the final authority over capital-at-risk actions.

---

## 0. Executive directive

Version 2.0 is not an equities system with options added to it. It is an **options-first portfolio management and execution system**. The underlying stock or ETF is analyzed as the driver of an options position, but the object ranked, sized, executed, monitored, and learned from is the complete options trade: underlying, strategy, expiration, strikes, quantity, net debit or credit, maximum loss, Greeks, liquidity, catalyst exposure, and exit plan.

The system's competitive objective is:

> Identify short-duration options opportunities where the expected benefit from directional movement, convexity, volatility behavior, and timing materially exceeds expected theta decay, spread cost, slippage, event risk, and portfolio risk.

The system must never claim that it can identify perfect entries or exits. It should maximize expected long-run, risk-adjusted performance under hard constraints while being willing to hold cash when no candidate has a sufficient edge.

The build must preserve the strongest elements of Version 1:

- Hard-coded guardrails outside prompts
- Separation of idea generation from risk control
- Settled-cash controls for a cash account
- Shadow testing and calibration
- Versioned configuration
- Full decision and order audit trails
- Paper-first rollout
- Read-only monitoring dashboard
- Model failover and deterministic exit protection

It must replace Version 1's equity assumptions with options-native data, scoring, sizing, risk, execution, monitoring, learning, and reporting.

---

## 1. Scope and non-goals

### 1.1 In scope

The system will:

- Analyze broad-market, sector, underlying, options-chain, volatility, liquidity, and catalyst conditions.
- Scan optionable equities and ETFs, then rank complete trade structures rather than merely ranking symbols.
- Specialize in positions normally opened with 7-28 DTE.
- Support only order types confirmed at runtime through the Robinhood Trading MCP.
- Prefer defined-risk structures when supported and when their expected value is superior.
- Maintain a portfolio-level view of delta, gamma, theta, vega, concentration, correlation, liquidity, and event risk.
- Apply code-enforced risk limits before any order reaches the broker.
- Reconcile every staged, submitted, accepted, rejected, partially filled, filled, canceled, expired, assigned, or otherwise changed order state.
- Preserve a complete audit trail sufficient to reconstruct every decision.
- Learn only through statistically controlled calibration and shadow testing.

### 1.2 Explicit non-goals

The system is not:

- A public SaaS platform
- A multi-user or multi-tenant product
- A guarantee of profitability
- A high-frequency trading engine
- A market maker
- An unrestricted self-modifying strategy
- A system that allows an LLM to bypass deterministic controls
- A system that assumes Robinhood supports every listed options strategy
- A replacement for the user's legal, tax, or financial judgment

### 1.3 Single-user does not mean low-grade

Remove customer-facing complexity, not engineering discipline. Version 2.0 does not need billing, tenant isolation, customer onboarding, public APIs, or organization administration. It still needs production-style reliability, security, testing, observability, recovery, auditability, and capital protection.

---

## 2. Core design principles

1. **Options are first-class objects.** Every candidate is a proposed options structure, not just a bullish or bearish opinion on an underlying.
2. **Deterministic calculations are services, not agents.** Greeks, payoff diagrams, spread width, maximum loss, buying-power checks, settlement checks, exposure aggregation, and guardrails are pure code.
3. **Reasoning agents interpret; they do not perform trusted arithmetic.** Agents consume validated analytics and produce structured judgments.
4. **Independent risk veto.** A trade that passes research can still be rejected by the deterministic validator or the Risk Officer.
5. **No-trade is a valid decision.** Cash is an active allocation when opportunity quality is insufficient.
6. **Every candidate competes for capital.** The system ranks opportunity cost across the full eligible universe.
7. **Portfolio risk dominates single-trade attractiveness.** A strong trade can be rejected if it duplicates existing exposure.
8. **Exits are planned at entry.** Each approved trade must have price, underlying, volatility, time, and event-based exit conditions.
9. **Model outputs are untrusted inputs.** They are schema-validated, logged, calibrated, and bounded.
10. **Autonomy is earned in stages.** Research -> paper -> approval-only -> narrowly bounded automation.

---

## 3. Hard-coded guardrails

All values below must live in immutable runtime configuration or code-protected policy files. Agents may recommend changes, but cannot apply them. Initial defaults are conservative placeholders and must be reviewed against actual account size, broker rules, option permissions, and paper-test evidence before live use.

```python
# config/risk_policy.py
PAPER_TRADING = True
ORDER_MODE = "research_only"  # research_only | approval_required | bounded_autonomy
ALLOW_LIVE_ORDERS = False

TARGET_DTE_MIN = 7
TARGET_DTE_MAX = 28
ABSOLUTE_DTE_MIN = 5
ABSOLUTE_DTE_MAX = 35

MAX_RISK_PER_TRADE_PCT = 0.01          # 1% of account equity at maximum loss
MAX_TOTAL_OPEN_RISK_PCT = 0.05         # sum of defined maximum losses
MAX_DAILY_REALIZED_LOSS_PCT = 0.02
MAX_DAILY_EQUITY_DRAWDOWN_PCT = 0.025
MAX_WEEKLY_DRAWDOWN_PCT = 0.05
MAX_PEAK_TO_TROUGH_DRAWDOWN_PCT = 0.10
MAX_CONCURRENT_POSITIONS = 3
MAX_CORRELATED_CLUSTER_RISK_PCT = 0.02
MAX_SINGLE_UNDERLYING_RISK_PCT = 0.015
MAX_SINGLE_SECTOR_RISK_PCT = 0.03

MIN_OPTION_OPEN_INTEREST = 100
MIN_OPTION_DAILY_VOLUME = 20
MAX_BID_ASK_SPREAD_PCT = 0.12
MAX_QUOTE_AGE_SECONDS = 5
MAX_UNDERLYING_DATA_AGE_SECONDS = 10
MIN_CONTRACT_PRICE = 0.10
MAX_CONTRACT_PRICE_PCT_OF_EQUITY = 0.03

MIN_OPPORTUNITY_SCORE = 75.0
MIN_CALIBRATED_PROBABILITY = 0.60
MIN_EXPECTED_REWARD_TO_RISK = 1.5
MIN_EXPECTED_VALUE_AFTER_COSTS = 0.0

MAX_NET_ABS_DELTA_PCT = 0.35
MAX_PORTFOLIO_GAMMA = None             # derive after paper calibration
MAX_DAILY_THETA_BURN_PCT = 0.004
MAX_ABS_VEGA_PCT = None                # derive after paper calibration

ENFORCE_SETTLED_CASH_ONLY = True
REQUIRE_DEFINED_MAX_LOSS = True
ALLOW_UNDEFINED_RISK_STRATEGIES = False
ALLOW_NAKED_SHORT_OPTIONS = False
ALLOW_MARKET_ORDERS = False
ALLOW_ZERO_DTE = False
ALLOW_EARNINGS_HOLD = False
ALLOW_NEW_ENTRY_DURING_FAILOVER = False

REQUIRE_MANUAL_RESUME_AFTER_HALT = True
REQUIRE_HUMAN_APPROVAL_FOR_CONFIG_PROMOTION = True
REQUIRE_HUMAN_APPROVAL_FOR_EVERY_LIVE_ORDER = True
```

### 3.1 Guardrail precedence

The decision order is mandatory:

1. System health and data freshness
2. Broker/account capability validation
3. Settlement and buying-power validation
4. Strategy permission validation
5. Per-trade maximum-loss validation
6. Portfolio exposure validation
7. Daily/weekly/drawdown circuit breakers
8. Liquidity and execution validation
9. Human approval policy
10. Order submission

No downstream approval can override an upstream rejection.

### 3.2 Kill switches

Implement independent kill switches for:

- Global trading halt
- New-entry halt while allowing risk-reducing exits
- Broker degradation
- Market-data degradation
- Model-provider degradation
- Order-state uncertainty
- Excessive slippage
- Excessive reject/partial-fill rate
- Reconciliation mismatch
- Drawdown breach
- Manual emergency stop

The system must default to **no new entries** whenever the true account, position, quote, or order state is uncertain.

---

## 4. Target architecture

```text
┌──────────────────────────────────────────────────────────────────────┐
│ SESSION CONTROLLER / PORTFOLIO ORCHESTRATOR                          │
│ market calendar, schedules, state machine, health, audit correlation │
└──────────────┬───────────────────────────────────────────────────────┘
               │
┌──────────────▼───────────────────────────────────────────────────────┐
│ DETERMINISTIC DATA AND ANALYTICS SERVICES                            │
│ market data | option chains | Greeks | volatility | liquidity        │
│ payoff | expected move | event calendar | portfolio exposures        │
│ settlement | risk | scoring | order validation | reconciliation      │
└──────────────┬───────────────────────────────────────────────────────┘
               │ validated feature packets
┌──────────────▼───────────────────────────────────────────────────────┐
│ REASONING COMMITTEE                                                   │
│ 1 Market Regime Strategist                                            │
│ 2 Universe and Catalyst Researcher                                    │
│ 3 Technical Structure Analyst                                        │
│ 4 Volatility and Options Structure Specialist                         │
│ 5 Strategy Selection Specialist                                       │
│ 6 Portfolio Manager                                                    │
│ 7 Independent Risk Officer                                             │
│ 8 Position Management Analyst                                          │
│ 9 Performance and Calibration Auditor                                  │
└──────────────┬───────────────────────────────────────────────────────┘
               │ structured proposals only
┌──────────────▼───────────────────────────────────────────────────────┐
│ DETERMINISTIC TRADE GATE                                              │
│ schema -> capability -> freshness -> risk -> liquidity -> settlement  │
│ -> duplicate prevention -> approval policy                            │
└──────────────┬───────────────────────────────────────────────────────┘
               │ approved order intent
┌──────────────▼───────────────────────────────────────────────────────┐
│ ROBINHOOD MCP ADAPTER + ORDER STATE MACHINE                           │
│ preview/stage -> submit -> reconcile -> fill/partial/cancel/reject    │
└──────────────┬───────────────────────────────────────────────────────┘
               │
┌──────────────▼───────────────────────────────────────────────────────┐
│ CONTINUOUS POSITION/RISK MONITOR + JOURNAL + READ-ONLY DASHBOARD      │
└──────────────────────────────────────────────────────────────────────┘
```

### 4.1 Services versus agents

Use a deterministic service when the task has a correct computational result. Use an agent when the task requires interpretation under uncertainty.

| Capability | Implementation |
|---|---|
| Black-Scholes/other Greeks from available inputs | Deterministic service |
| Broker-provided Greeks normalization | Deterministic service |
| Maximum loss and payoff calculation | Deterministic service |
| Bid-ask and liquidity metrics | Deterministic service |
| Portfolio exposure aggregation | Deterministic service |
| Market regime interpretation | Reasoning agent using deterministic features |
| Catalyst relevance | Reasoning agent with sourced evidence |
| Thesis quality and invalidation | Reasoning agent |
| Strategy selection among allowed structures | Reasoning agent plus deterministic optimizer |
| Final trade permission | Deterministic validator |
| Order submission and reconciliation | Deterministic state machine |

---

## 5. Deterministic service layer

### 5.1 Broker capability discovery service

At startup and before any live session, inspect the connected Robinhood MCP tools and record:

- Available account and balance fields
- Options-chain and quote availability
- Available Greeks
- Supported single-leg and multi-leg order types
- Limit-order support and price increments
- Preview/stage/submit semantics
- Approval behavior
- Cancel/replace capabilities
- Order-state fields
- Assignment/exercise/expiration information

Do not assume that a structure is tradable merely because Robinhood supports it in another interface. Create a `broker_capabilities` snapshot and reject unsupported strategies before research spends tokens evaluating them.

### 5.2 Market-data service

Normalize and timestamp:

- Underlying quotes, OHLCV, spreads, and session status
- Option chains by expiration and strike
- Contract bid, ask, midpoint, last, volume, open interest, implied volatility, and Greeks when available
- Corporate actions
- Earnings and major scheduled catalysts
- Broad-market, sector, volatility-index, and rates proxies

Every datum must contain `source`, `observed_at`, `received_at`, `age_seconds`, and a quality flag. Never silently combine stale and current data.

### 5.3 Options analytics service

For every contract and supported structure calculate or normalize:

- Delta, gamma, theta, vega, rho when available
- Dollar delta, dollar gamma, daily theta dollars, and vega dollars
- Gamma-to-theta ratio
- Theta as percentage of premium
- Breakeven(s)
- Maximum gain and loss when defined
- Expected payoff under configurable scenarios
- Probability proxy metrics, clearly labeled as estimates
- Moneyness and distance from spot
- Distance from expected move
- IV term structure and skew
- Spread percentage and estimated round-trip cost
- Open-interest and volume quality
- Contract multiplier and quantity-normalized exposure

If Greeks must be calculated, store input assumptions and distinguish calculated values from broker-supplied values.

### 5.4 Volatility service

Calculate:

- Realized volatility over multiple windows
- Implied versus realized volatility spread
- IV rank/percentile only when enough historical data exists
- Term structure across eligible expirations
- Put/call skew
- Expected move by expiration
- Event-adjusted volatility flags
- Volatility regime and change rate

### 5.5 Technical and market-structure service

Produce reproducible features, not subjective chart descriptions:

- Trend across multiple timeframes
- Momentum and acceleration
- Support/resistance zones
- Relative strength versus sector and benchmark
- ATR and normalized ATR
- Gap behavior
- Volume regime and unusual volume
- Breakout, pullback, compression, and mean-reversion features
- Distance from VWAP and moving averages
- Intraday liquidity windows

### 5.6 Liquidity and execution-cost service

Calculate:

- Absolute and percentage spread
- Midpoint quality
- Size at bid/ask when available
- Open interest and volume thresholds
- Estimated slippage under contract quantity
- Price increment compatibility
- Expected round-trip friction
- Partial-fill risk

### 5.7 Portfolio analytics service

Aggregate in real time:

- Net and gross delta
- Gamma by underlying and portfolio
- Daily theta burn
- Vega exposure
- Defined maximum open loss
- Exposure by underlying, sector, direction, strategy, expiration week, and catalyst
- Pairwise and cluster correlation
- Liquidity-at-risk
- Concentration and stress scenarios

### 5.8 Opportunity scoring service

Create a deterministic base score from normalized features. Agent judgments may adjust bounded components, but cannot overwrite raw metrics.

```python
@dataclass(frozen=True)
class OpportunityScore:
    directional_edge: float
    gamma_efficiency: float
    theta_efficiency: float
    volatility_fit: float
    liquidity: float
    catalyst_quality: float
    technical_structure: float
    market_regime_fit: float
    portfolio_fit: float
    execution_quality: float
    expected_value_after_costs: float
    risk_penalty: float
    total: float
```

Recommended conceptual weighting for initial paper testing, not live optimization:

```text
Directional/technical edge        15
Gamma efficiency                  12
Theta efficiency                  12
Volatility fit                    10
Liquidity/execution quality       15
Catalyst quality                   8
Market regime fit                  8
Portfolio fit                     10
Expected value after costs        10
Risk penalties                  subtractive
```

The score is a ranking aid, not proof of edge. Store every component.

### 5.9 Opportunity-cost engine

For every eligible candidate calculate:

- Incremental expected return
- Incremental maximum loss
- Incremental portfolio Greeks
- Correlation with current positions
- Capital consumed
- Estimated execution friction
- Expected return per unit of risk
- Expected return per unit of settled cash
- Expected return per unit of theta
- Replacement value versus the weakest current position

A candidate should not be approved merely because it passes thresholds. It must be one of the best available uses of limited risk budget.

---

## 6. Reasoning committee

All agents return schema-validated JSON and cite the data packet they used. No agent may call the broker execution tools directly.

### Agent 1 - Market Regime Strategist

Classifies the environment using validated features:

- Trending bullish
- Trending bearish
- Range-bound
- High-volatility expansion
- Volatility compression
- Breakout-prone
- Mean-reverting
- Event-dominated
- Risk-off/dislocated

Output includes regime, confidence, supporting features, contradictory evidence, permitted strategy families, and strategy families to avoid.

### Agent 2 - Universe and Catalyst Researcher

Evaluates:

- Earnings, product, regulatory, litigation, macro, analyst, corporate-action, and sector catalysts
- Timing relative to the proposed expiration
- Whether the catalyst is scheduled, speculative, already priced, or stale
- Gap and IV-crush risk

It must distinguish facts from interpretations and record source timestamps.

### Agent 3 - Technical Structure Analyst

Interprets deterministic technical features and produces:

- Directional thesis
- Entry zone
- Underlying invalidation level
- Expected move window
- Time horizon
- Alternative scenario

It cannot recommend a contract.

### Agent 4 - Volatility and Options Structure Specialist

Interprets:

- IV versus realized volatility
- Term structure and skew
- Gamma/theta trade-off
- Contract liquidity
- Event volatility
- Whether premium appears relatively expensive or inexpensive

It proposes allowed expiration and delta bands, not an order.

### Agent 5 - Strategy Selection Specialist

Selects among broker-supported, policy-permitted structures. Initial preference order:

1. Defined-risk debit spread when directional conviction is high and single-leg premium is inefficient
2. Long call or long put when convexity, liquidity, and expected move justify premium risk
3. Defined-risk credit spread only when supported, volatility conditions justify premium selling, assignment risks are handled, and account permissions allow it
4. No trade

The specialist must compare at least two feasible structures and explain why the selected structure has the best expected value after costs and risk.

### Agent 6 - Portfolio Manager

Ranks candidates against available risk budget and current exposures. It may approve a proposal for the deterministic gate, reduce its requested risk, defer it, recommend replacing an existing position, or hold cash.

### Agent 7 - Independent Risk Officer

Reviews assumptions, contradiction, event risk, liquidity, crowding, tail risk, and failure modes. It has veto authority at the reasoning layer but cannot relax code limits. Required output: `approve`, `approve_with_reduction`, or `veto` with enumerated reasons.

### Agent 8 - Position Management Analyst

Reviews open positions using fresh data and asks:

> Is the remaining expected opportunity greater than the remaining decay, volatility risk, event risk, and execution cost?

It can recommend hold, reduce, take profit, exit, roll, or hedge, but only strategies supported by policy and broker capabilities may proceed. Hard stops and circuit breakers remain pure code.

### Agent 9 - Performance and Calibration Auditor

Evaluates performance by:

- Strategy family
- Regime
- DTE at entry
- DTE at exit
- Delta band
- Gamma/theta band
- IV rank/term structure
- Sector and underlying
- Catalyst type
- Entry time and holding duration
- Agent contribution
- Opportunity-score band
- Fill quality and slippage
- Exit reason

It proposes changes only when sample-size and significance gates are met. Every proposal enters shadow testing and requires human promotion.

---

## 7. Strategy universe and eligibility

### 7.1 Supported strategy registry

Build a registry rather than embedding strategy assumptions throughout the code.

```python
STRATEGY_REGISTRY = {
    "long_call": {
        "defined_risk": True,
        "legs": 1,
        "requires": ["buy_to_open_call"],
    },
    "long_put": {
        "defined_risk": True,
        "legs": 1,
        "requires": ["buy_to_open_put"],
    },
    "bull_call_debit_spread": {
        "defined_risk": True,
        "legs": 2,
        "requires": ["multi_leg_options", "debit_spread"],
    },
    "bear_put_debit_spread": {
        "defined_risk": True,
        "legs": 2,
        "requires": ["multi_leg_options", "debit_spread"],
    },
    "put_credit_spread": {
        "defined_risk": True,
        "legs": 2,
        "requires": ["multi_leg_options", "credit_spread", "assignment_handling"],
    },
    "call_credit_spread": {
        "defined_risk": True,
        "legs": 2,
        "requires": ["multi_leg_options", "credit_spread", "assignment_handling"],
    },
}
```

At runtime, intersect this registry with broker capabilities and account permissions. Unsupported structures must disappear from the candidate universe.

### 7.2 DTE rules

- Preferred entry: 7-28 DTE
- Reject below 5 DTE
- Reject above 35 DTE unless a manually approved research experiment
- Do not open a position that cannot be safely managed before expiration
- Establish mandatory review checkpoints as DTE declines
- Default to closing before expiration unless a specific, tested policy says otherwise

### 7.3 Strike selection

Strike selection must balance:

- Delta exposure
- Gamma sensitivity
- Theta burden
- Liquidity
- Breakeven distance
- Expected underlying move
- Spread width and defined loss
- Event and assignment risk

Do not select strikes solely because they are cheap.

---

## 8. Entry process

A trade proposal must pass the following pipeline:

1. Session and market state valid
2. Broker capabilities current
3. Data packet fresh and complete
4. Underlying passes universe filters
5. Market regime allows the strategy family
6. Catalyst risk classified
7. Technical thesis and invalidation defined
8. Eligible expirations and strikes generated
9. Candidate structures priced and analyzed
10. Deterministic opportunity score calculated
11. Strategy specialist compares alternatives
12. Portfolio manager ranks capital allocation
13. Independent risk officer reviews
14. Deterministic trade gate validates all hard limits
15. Human approval obtained when required
16. Limit order staged/submitted
17. Order state reconciled until terminal state
18. Position and planned exits written atomically to the journal

### 8.1 Required trade proposal schema

```json
{
  "proposal_id": "uuid",
  "underlying": "SPY",
  "direction": "bullish",
  "strategy": "bull_call_debit_spread",
  "expiration": "YYYY-MM-DD",
  "dte": 14,
  "legs": [
    {"side": "buy", "type": "call", "strike": 600, "quantity": 1},
    {"side": "sell", "type": "call", "strike": 605, "quantity": 1}
  ],
  "limit_price": 1.85,
  "max_loss": 185.00,
  "max_gain": 315.00,
  "breakevens": [601.85],
  "net_delta": 0.24,
  "net_gamma": 0.04,
  "net_theta_daily": -6.20,
  "net_vega": 8.10,
  "liquidity": {},
  "thesis": {},
  "invalidation": {},
  "exit_plan": {},
  "opportunity_score": {},
  "portfolio_impact": {},
  "risk_officer_decision": {},
  "data_snapshot_ids": [],
  "model_versions": {},
  "config_version": "uuid"
}
```

---

## 9. Position sizing

Size positions by maximum loss, never merely by premium paid or notional value.

```python
def calculate_contract_quantity(
    account_equity: Decimal,
    settled_cash: Decimal,
    candidate_max_loss_per_unit: Decimal,
    current_open_risk: Decimal,
    correlated_cluster_risk: Decimal,
) -> int:
    per_trade_budget = account_equity * Decimal(str(MAX_RISK_PER_TRADE_PCT))
    portfolio_remaining = (
        account_equity * Decimal(str(MAX_TOTAL_OPEN_RISK_PCT)) - current_open_risk
    )
    cluster_remaining = (
        account_equity * Decimal(str(MAX_CORRELATED_CLUSTER_RISK_PCT))
        - correlated_cluster_risk
    )
    cash_remaining = settled_cash

    budget = min(per_trade_budget, portfolio_remaining, cluster_remaining, cash_remaining)
    if budget <= 0 or candidate_max_loss_per_unit <= 0:
        return 0
    return max(0, int(budget // candidate_max_loss_per_unit))
```

Then apply portfolio Greek limits, liquidity limits, and broker buying-power validation. Any resulting quantity of zero means no trade.

For debit trades, reserve the full debit plus fees. For credit spreads, reserve broker-required collateral and independently calculate maximum loss. Never rely solely on an LLM's representation of defined risk.

---

## 10. Exit and position-management framework

Every position must have five exit dimensions.

### 10.1 Premium-based exits

- Hard maximum-loss threshold
- Planned profit target or scaling rules
- Slippage-aware limit prices

### 10.2 Underlying-based exits

- Thesis invalidation level
- Breakout failure
- Trend reversal
- Support/resistance violation

### 10.3 Time-based exits

- Maximum holding duration
- Mandatory review at configured DTE thresholds
- Exit before expiration by default
- Accelerating theta alert

### 10.4 Volatility-based exits

- IV crush
- Volatility expansion benefiting the position
- Skew or term-structure regime change
- Remaining vega risk versus thesis

### 10.5 Event-based exits

- Catalyst completed
- New material event invalidates thesis
- Earnings or scheduled event enters prohibited window
- Trading halt or abnormal liquidity

### 10.6 Deterministic emergency exits

Pure code must be able to trigger risk-reducing exits without an LLM for:

- Maximum-loss breach
- Portfolio drawdown breach
- DTE/expiration safety threshold
- Data or broker state indicating assignment/exercise danger
- Position state mismatch

Limit orders remain preferred. If the broker lacks the necessary order mechanism, the system must alert and halt rather than silently improvise.

---

## 11. Cash-account settlement controls

Retain Version 1's settled-cash invariant, but adapt it to options.

- New debit positions may use only settled cash.
- Collateral for credit spreads, if supported, must be covered according to broker requirements and internal maximum-loss calculations.
- Unsettled proceeds are unavailable for new entries.
- Every order preview must be checked against current settled cash immediately before submission.
- The system must record settlement dates and projected available cash.
- Risk-reducing exits are not blocked merely because proceeds will become unsettled.

```python
def assert_debit_trade_is_covered(total_debit, settled_cash):
    if ENFORCE_SETTLED_CASH_ONLY and total_debit > settled_cash:
        raise TradeRejected("insufficient_settled_cash")
```

Broker and regulatory rules can change. Keep settlement policy values configurable and require explicit review when broker documentation or account behavior changes.

---

## 12. Order execution and reconciliation

### 12.1 Execution principles

- Limit orders only
- Idempotency keys on every order intent
- Preview before submit when supported
- Re-read account and positions before submit
- Validate quote freshness at the last possible moment
- Never chase a fill beyond a configured maximum price
- Cancel stale orders after a strategy-specific timeout
- Handle partial fills explicitly
- Prevent duplicate legs or duplicate submissions
- No new entry while any relevant order state is uncertain

### 12.2 Order state machine

```text
CREATED
 -> VALIDATED
 -> AWAITING_APPROVAL
 -> STAGED
 -> SUBMITTED
 -> OPEN
 -> PARTIALLY_FILLED
 -> FILLED
 -> CANCELED
 -> REJECTED
 -> EXPIRED
 -> RECONCILIATION_REQUIRED
```

Transitions must be append-only in an order-event table. The current state is derived from events, not overwritten without history.

### 12.3 Multi-leg atomicity

If multi-leg strategies are supported, use broker-native multi-leg orders. Never emulate a defined-risk spread by independently submitting legs unless an explicit, extensively tested legging policy exists. Version 2.0 should reject a strategy rather than expose the account to unintended naked risk.

### 12.4 Approval

Research mode produces no order. Approval mode stages a complete proposal and requires explicit user approval through the broker-supported approval path or a separately authenticated local control surface. The read-only dashboard remains incapable of approval.

---

## 13. Learning, calibration, and model risk

### 13.1 A loss is not automatically an error

Measure calibration over sufficiently large samples. Do not tune from individual outcomes.

### 13.2 New options-native calibration dimensions

Create buckets by:

- Strategy
- Regime
- DTE band
- Delta band
- Gamma/theta band
- IV rank and term-structure state
- Spread-quality band
- Catalyst type
- Underlying/sector
- Entry hour
- Holding duration
- Opportunity-score band
- Model and prompt version

### 13.3 Metrics

Track:

- Win rate
- Average win/loss
- Expectancy after costs
- Profit factor
- Maximum adverse excursion
- Maximum favorable excursion
- Slippage versus midpoint
- Fill rate and time to fill
- Return on maximum risk
- Return per unit of theta
- Return by delta/gamma exposure
- Brier score or another proper score for probability calibration
- Drawdown and recovery duration

### 13.4 Promotion process

1. Auditor proposes a bounded change with evidence.
2. A new immutable config version is created in shadow state.
3. Shadow config evaluates the same future opportunities without live orders.
4. Compare to control using minimum sample size, time window, costs, and statistical uncertainty.
5. Human reviews.
6. Promotion is logged or rejected.
7. Rollback remains available.

No agent can alter hard guardrails.

---

## 14. Data model

Use PostgreSQL for the target system; SQLite may be used only for the first local prototype if migrations preserve compatibility.

### 14.1 Core tables

```sql
CREATE TABLE broker_capability_snapshots (
    id UUID PRIMARY KEY,
    observed_at TIMESTAMPTZ NOT NULL,
    account_id_hash TEXT NOT NULL,
    capabilities JSONB NOT NULL,
    source_version TEXT,
    is_current BOOLEAN NOT NULL
);

CREATE TABLE market_data_snapshots (
    id UUID PRIMARY KEY,
    symbol TEXT NOT NULL,
    instrument_type TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    source TEXT NOT NULL,
    payload JSONB NOT NULL,
    quality_flags JSONB NOT NULL
);

CREATE TABLE option_contract_snapshots (
    id UUID PRIMARY KEY,
    underlying TEXT NOT NULL,
    option_symbol TEXT NOT NULL,
    expiration DATE NOT NULL,
    strike NUMERIC NOT NULL,
    option_type TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    bid NUMERIC,
    ask NUMERIC,
    midpoint NUMERIC,
    volume BIGINT,
    open_interest BIGINT,
    implied_volatility NUMERIC,
    delta NUMERIC,
    gamma NUMERIC,
    theta NUMERIC,
    vega NUMERIC,
    greek_source TEXT,
    raw_payload JSONB
);

CREATE TABLE opportunity_candidates (
    id UUID PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    underlying TEXT NOT NULL,
    strategy TEXT NOT NULL,
    expiration DATE NOT NULL,
    legs JSONB NOT NULL,
    analytics JSONB NOT NULL,
    score_components JSONB NOT NULL,
    total_score NUMERIC NOT NULL,
    status TEXT NOT NULL,
    rejection_reasons JSONB,
    config_version_id UUID NOT NULL
);

CREATE TABLE trade_proposals (
    id UUID PRIMARY KEY,
    candidate_id UUID REFERENCES opportunity_candidates(id),
    created_at TIMESTAMPTZ NOT NULL,
    proposal JSONB NOT NULL,
    portfolio_impact JSONB NOT NULL,
    risk_decision JSONB NOT NULL,
    approval_status TEXT NOT NULL,
    config_version_id UUID NOT NULL
);

CREATE TABLE orders (
    id UUID PRIMARY KEY,
    proposal_id UUID REFERENCES trade_proposals(id),
    idempotency_key TEXT UNIQUE NOT NULL,
    broker_order_id TEXT,
    current_state TEXT NOT NULL,
    submitted_at TIMESTAMPTZ,
    raw_request JSONB,
    raw_response JSONB
);

CREATE TABLE order_events (
    id UUID PRIMARY KEY,
    order_id UUID REFERENCES orders(id),
    event_at TIMESTAMPTZ NOT NULL,
    previous_state TEXT,
    new_state TEXT NOT NULL,
    broker_payload JSONB,
    reason TEXT
);

CREATE TABLE positions (
    id UUID PRIMARY KEY,
    proposal_id UUID REFERENCES trade_proposals(id),
    underlying TEXT NOT NULL,
    strategy TEXT NOT NULL,
    expiration DATE NOT NULL,
    legs JSONB NOT NULL,
    opened_at TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ,
    entry_net_price NUMERIC NOT NULL,
    exit_net_price NUMERIC,
    quantity INT NOT NULL,
    max_loss NUMERIC NOT NULL,
    status TEXT NOT NULL,
    exit_plan JSONB NOT NULL
);

CREATE TABLE position_snapshots (
    id UUID PRIMARY KEY,
    position_id UUID REFERENCES positions(id),
    observed_at TIMESTAMPTZ NOT NULL,
    marked_value NUMERIC,
    unrealized_pnl NUMERIC,
    net_delta NUMERIC,
    net_gamma NUMERIC,
    net_theta NUMERIC,
    net_vega NUMERIC,
    dte INT,
    liquidity JSONB,
    thesis_state JSONB
);

CREATE TABLE portfolio_snapshots (
    id UUID PRIMARY KEY,
    observed_at TIMESTAMPTZ NOT NULL,
    total_equity NUMERIC NOT NULL,
    settled_cash NUMERIC NOT NULL,
    unsettled_cash NUMERIC NOT NULL,
    open_risk NUMERIC NOT NULL,
    net_delta NUMERIC,
    net_gamma NUMERIC,
    daily_theta NUMERIC,
    net_vega NUMERIC,
    high_water_mark NUMERIC,
    drawdown NUMERIC,
    is_paper BOOLEAN NOT NULL
);

CREATE TABLE agent_decisions (
    id UUID PRIMARY KEY,
    correlation_id UUID NOT NULL,
    agent_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    model_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_snapshot_ids JSONB NOT NULL,
    output JSONB NOT NULL,
    validation_result JSONB NOT NULL,
    latency_ms INT,
    token_usage JSONB
);

CREATE TABLE strategy_config_versions (
    id UUID PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    parameters JSONB NOT NULL,
    status TEXT NOT NULL,
    proposed_by TEXT,
    evidence JSONB,
    approved_by TEXT,
    approved_at TIMESTAMPTZ
);

CREATE TABLE calibration_results (
    id UUID PRIMARY KEY,
    dimension_key JSONB NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    sample_size INT NOT NULL,
    metrics JSONB NOT NULL,
    proposed_action JSONB
);

CREATE TABLE system_events (
    id UUID PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    severity TEXT NOT NULL,
    component TEXT NOT NULL,
    event_type TEXT NOT NULL,
    correlation_id UUID,
    payload JSONB NOT NULL
);
```

### 14.2 Data retention

Retain the exact market and analytics snapshot used for every decision. Do not reconstruct decisions later from updated market data.

---

## 15. Session orchestration

Implement a state machine rather than a loose cron script.

```text
OFFLINE
 -> STARTUP_VALIDATION
 -> PREMARKET_RESEARCH
 -> MARKET_OPEN_OBSERVATION
 -> ENTRY_WINDOW
 -> POSITION_MANAGEMENT
 -> ENTRY_PAUSED
 -> MARKET_CLOSE_RECONCILIATION
 -> POSTMARKET_AUDIT
 -> OFFLINE

Any state -> DEGRADED
Any state -> HALTED
```

### 15.1 Startup validation

Before the first scan:

- Confirm market calendar and session
- Confirm system clock synchronization
- Confirm database health
- Confirm broker authentication
- Refresh capability snapshot
- Reconcile account, positions, and open orders
- Validate market-data feeds
- Load active config and verify signature/hash
- Confirm paper/live state visibly
- Run kill-switch self-test

### 15.2 Scheduling

Use event-driven monitoring where possible. Scanning can run on a bounded cadence, but open-position hard-risk monitoring should be more frequent than new-entry research. Avoid unnecessary LLM calls; deterministic services update continuously and invoke agents only when a meaningful state change occurs.

---

## 16. Model architecture and failover

Do not hardcode fictional or unavailable model names. Centralize model aliases and map them to currently supported Anthropic model IDs during setup.

```python
AGENT_MODELS = {
    "market_regime": "${CLAUDE_REASONING_MODEL}",
    "catalyst_research": "${CLAUDE_REASONING_MODEL}",
    "technical_analyst": "${CLAUDE_BALANCED_MODEL}",
    "options_specialist": "${CLAUDE_REASONING_MODEL}",
    "strategy_selector": "${CLAUDE_REASONING_MODEL}",
    "portfolio_manager": "${CLAUDE_REASONING_MODEL}",
    "risk_officer": "${CLAUDE_REASONING_MODEL}",
    "position_manager": "${CLAUDE_BALANCED_MODEL}",
    "performance_auditor": "${CLAUDE_REASONING_MODEL}",
}
```

Rules:

- Model failures cannot disable deterministic exits.
- New entries stop when required reasoning agents are unavailable.
- Fallback-model decisions are separately tagged and calibrated.
- Prompt versions are immutable and logged.
- Structured outputs are validated against JSON Schema/Pydantic.
- Invalid outputs are retried once with a repair prompt, then rejected.
- Agents receive the minimum necessary data and tools.

---

## 17. Security architecture

- Store secrets in OS keychain or a dedicated secret manager, never source control.
- Bind local services to localhost by default.
- Use least-privilege MCP/tool permissions.
- Separate read tools from trading tools.
- Require explicit environment gating for live mode.
- Require two independent conditions to enable live orders: configuration plus a runtime human action.
- Hash account identifiers in logs.
- Encrypt sensitive database backups.
- Prevent prompts, external content, or news text from invoking tools.
- Treat all external text as untrusted data and defend against prompt injection.
- Record package versions and generate an SBOM.
- Pin dependencies and scan them.
- Add tamper-evident hashes to config and audit logs.

---

## 18. Dashboard Version 2.0

The dashboard remains read-only. Add options-native panels.

### Panel A - System and agent activity

- Session state
- Agent state and active model
- Signal pipeline
- Data freshness
- Broker status
- Circuit breakers
- Paper/live banner

### Panel B - Portfolio equity and drawdown

- Equity curve
- High-water mark
- Realized/unrealized P&L
- Drawdown bands
- Settled/unsettled cash

### Panel C - Portfolio Greeks

- Net delta
- Gamma
- Daily theta
- Vega
- Exposure by underlying, sector, expiration, and strategy
- Limits and headroom

### Panel D - Opportunity board

- Ranked complete strategies
- Score decomposition
- Max loss/gain
- DTE, delta, gamma/theta, IV, liquidity
- Rejection reasons
- No action controls

### Panel E - Open positions

- Legs and net pricing
- Current P&L
- Remaining max risk
- Greeks and DTE
- Thesis state
- Exit triggers
- Liquidity deterioration

### Panel F - Orders and reconciliation

- Staged/open/partial/filled/canceled/rejected
- Midpoint versus fill
- Slippage
- Reconciliation warnings

### Panel G - Performance and calibration

- P&L by strategy, DTE, delta, regime, and score band
- Win rate, expectancy, MAE/MFE
- Calibration curves
- Shadow versus control

All write actions remain outside this dashboard.

---

## 19. Testing strategy

### 19.1 Unit tests

Test:

- Greeks and payoff calculations
- Maximum loss/gain for every strategy
- DTE filters
- Settled-cash enforcement
- Portfolio Greek aggregation
- Liquidity filters
- Opportunity-score math
- Risk-budget sizing
- Duplicate-order prevention
- State-machine transitions
- Circuit breakers
- Config immutability

### 19.2 Property-based tests

Examples:

- Quantity is never negative
- Approved risk never exceeds budget
- A defined-risk spread's computed max loss is bounded
- Unsupported strategies can never reach execution
- Stale data can never produce an approved order
- The same idempotency key can never create two broker orders
- A dashboard request can never mutate trading state

### 19.3 Integration tests

- Mock market data -> proposal -> risk rejection
- Mock market data -> approval -> staged paper order
- Partial fill and cancel flow
- Broker timeout and reconciliation
- Model outage while positions are open
- Database restart and state recovery
- Market-data staleness
- Cash settlement transition

### 19.4 Historical simulation

Avoid naive backtests. Include:

- Point-in-time option chains where available
- Bid/ask, not last price
- Slippage and commissions/fees
- Survivorship-bias controls
- Corporate actions
- Event timing
- Realistic entry and exit availability
- Walk-forward evaluation
- Separate training, validation, and final holdout periods

If high-quality historical options data is unavailable, label results as limited and do not use them to justify live autonomy.

### 19.5 Chaos tests

- Broker returns inconsistent state
- Quotes freeze
- Database write fails
- Model returns malformed output
- Network drops during submission
- Duplicate webhook/poll response
- Clock skew
- Process restart with open positions

---

## 20. Deployment and operating modes

### Mode 0 - Offline research

No broker connection required. Historical or delayed data only.

### Mode 1 - Live research

Reads live data and account state but cannot stage or submit orders.

### Mode 2 - Paper execution

Full lifecycle with simulated fills and paper portfolio.

### Mode 3 - Approval-required live

Every live order requires explicit user approval. No autonomous entries.

### Mode 4 - Bounded autonomy

Only after extensive validation. Restrict by strategy, underlyings, maximum risk, trading windows, and daily count. Human approval remains required for policy/config changes and any trade outside the approved envelope.

Graduation is always manual and documented.

---

## 21. Repository structure

```text
options-firm-v2/
  README.md
  CLAUDE.md
  pyproject.toml
  uv.lock
  .env.example
  config/
    models.py
    risk_policy.py
    strategy_registry.py
    environments.py
  src/
    domain/
      instruments.py
      strategies.py
      proposals.py
      orders.py
      positions.py
      portfolio.py
    data/
      market_data.py
      option_chains.py
      catalysts.py
      broker_capabilities.py
    analytics/
      greeks.py
      payoff.py
      volatility.py
      technicals.py
      liquidity.py
      opportunity_score.py
      portfolio_exposure.py
      stress.py
    agents/
      market_regime.py
      catalyst_research.py
      technical_analyst.py
      options_specialist.py
      strategy_selector.py
      portfolio_manager.py
      risk_officer.py
      position_manager.py
      performance_auditor.py
      schemas.py
    risk/
      policy.py
      trade_gate.py
      sizing.py
      settlement.py
      circuit_breakers.py
    execution/
      robinhood_mcp.py
      order_state_machine.py
      reconciliation.py
      idempotency.py
      paper_broker.py
    orchestration/
      session_controller.py
      workflows.py
      event_bus.py
      health.py
    persistence/
      models.py
      repositories.py
      migrations/
    api/
      read_only.py
      websockets.py
    dashboard/
      frontend/
    observability/
      logging.py
      metrics.py
      audit.py
  tests/
    unit/
    property/
    integration/
    chaos/
    fixtures/
  scripts/
    launch_paper.py
    reconcile.py
    emergency_halt.py
    export_audit.py
  docs/
    ARCHITECTURE_V2.md
    RISK_POLICY.md
    OPERATIONS_RUNBOOK.md
    BROKER_CAPABILITIES.md
    MODEL_CARD.md
    BUILD_LOG.md
```

---

## 22. Claude Code master build prompt

Copy the prompt below into Claude Code together with this document and the existing Version 1 repository.

### BEGIN CLAUDE CODE PROMPT

You are the lead architect and senior engineer responsible for converting the existing Version 1 personal equities trading system into **Version 2.0: a private, institutional-grade, options-first portfolio management and execution system**.

Read the entire attached Version 2.0 architecture specification before modifying code. Also inspect the existing repository and produce a traceability matrix showing which Version 1 components will be retained, rewritten, removed, or newly created.

#### Primary objective

Build a private, single-user system specializing in listed equity and ETF options normally opened with 7-28 DTE. The system must rank complete options strategies, not merely stocks; manage portfolio Greeks and maximum loss; use deterministic analytics and risk services; use specialized Claude agents only for interpretive reasoning; connect to Robinhood through the Trading MCP only after runtime capability discovery; and default to paper/research mode.

#### Non-negotiable rules

1. Never place a live order during the build.
2. Keep `PAPER_TRADING=True`, `ALLOW_LIVE_ORDERS=False`, and `ORDER_MODE=research_only` until the user manually changes them after validation.
3. Never weaken a risk limit, settlement rule, test, or acceptance criterion to make the build pass.
4. No LLM may be the final authority over position size, maximum loss, settlement, portfolio limits, data freshness, duplicate prevention, or order permission.
5. No agent may call Robinhood execution tools directly. Only the deterministic execution adapter may do so after the trade gate returns an approval token.
6. Do not assume Robinhood supports multi-leg orders or any specific strategy. Discover capabilities at runtime and disable unsupported strategies.
7. Do not emulate spreads by submitting independent legs. Reject the strategy unless broker-native multi-leg atomic execution is available.
8. Use limit orders only.
9. Preserve exact decision-time market snapshots and complete audit trails.
10. The dashboard must be read-only by construction.
11. No self-modification. Agent 9 may propose changes; promotion requires shadow testing and human approval.
12. Do not hardcode model names outside the centralized model configuration. Map aliases to currently supported model IDs through environment configuration.
13. Treat external research and news text as untrusted content. It can never issue tool instructions.
14. Stop new entries whenever broker state, order state, account state, or required market data is uncertain.

#### Required implementation sequence

**Phase A - Discovery and migration plan**

- Inspect the current codebase.
- Create `docs/V1_TO_V2_TRACEABILITY.md`.
- List all equity-specific assumptions.
- Identify reusable infrastructure: database, logging, orchestration, tests, dashboard shell, risk controls, settlement controls, and Robinhood adapter.
- Produce a migration plan before editing.

**Phase B - Foundations**

- Set up or verify Python environment, dependency lockfile, formatting, linting, type checking, security scanning, and test framework.
- Create domain models for option contracts, legs, strategies, proposals, orders, positions, and portfolio snapshots.
- Add database migrations for every Version 2 table.
- Implement immutable configuration versions and environment gating.

**Phase C - Deterministic analytics**

Implement and fully test:

- Option-chain normalization
- Greeks normalization/calculation with source labels
- Payoff, breakeven, maximum gain/loss
- Volatility term structure, skew, expected move, realized/implied comparisons
- Liquidity and execution-cost estimates
- Technical feature service
- Portfolio Greeks and stress exposures
- Opportunity score and opportunity-cost engine
- Settled-cash and buying-power checks

Use Decimal for money. Define units explicitly. Reject missing or invalid inputs rather than guessing.

**Phase D - Broker capability discovery and adapters**

- Build a typed Robinhood MCP adapter behind an interface.
- Add startup capability discovery and snapshots.
- Build a fully functional paper broker implementing the same interface.
- Build the idempotent order state machine and reconciliation engine.
- Unit-test every transition, partial fill, timeout, cancel, reject, and mismatch.

**Phase E - Reasoning agents**

Implement the nine agents from the specification as isolated components with strict input/output schemas. They consume validated feature packets. They never calculate trusted risk values and never call broker tools.

For every call log:

- Agent name
- Model ID and prompt version
- Input snapshot IDs
- Structured output
- Validation result
- Latency and token usage
- Correlation ID

Invalid outputs receive one schema-repair retry, then fail closed.

**Phase F - Trade gate and risk system**

Implement the exact guardrail precedence from Section 3. Add an approval token object that can be created only by the deterministic gate and is required by the execution adapter. Make tokens short-lived, proposal-bound, account-state-bound, and quote-snapshot-bound.

Write tests that attempt to bypass every guardrail and prove the bypass fails.

**Phase G - Position management**

- Implement premium-, underlying-, time-, volatility-, and event-based exit plans.
- Implement hard emergency exits without LLM dependency.
- Implement DTE and assignment-risk checkpoints.
- Implement no-new-entry degraded mode while still allowing risk-reducing exits.

**Phase H - Orchestration**

Build the session state machine, startup validation, market calendar, health checks, event bus, and workflows. Deterministic services update continuously; invoke LLM agents only on meaningful events or scheduled analysis windows.

**Phase I - Learning and calibration**

Fully implement options-native calibration buckets, MAE/MFE, slippage analysis, probability scoring, shadow configs, control comparisons, human promotion, and rollback. Do not stub this phase.

**Phase J - Dashboard**

Build all Version 2 panels over read-only API routes and WebSockets. Confirm there are no mutating endpoints and no dependency from the dashboard to the execution adapter.

**Phase K - Validation**

- Run formatting, linting, type checking, unit, property, integration, and chaos tests.
- Execute an end-to-end paper session using deterministic fixtures.
- Simulate model outage, market-data outage, broker timeout, partial fill, restart with open position, stale quote, duplicate order, settlement shortage, and drawdown halt.
- Generate `docs/SPEC_COMPLIANCE_REPORT.md` mapping every requirement to code and tests.
- Generate `docs/OPERATIONS_RUNBOOK.md` covering startup, shutdown, reconciliation, emergency halt, recovery, paper/live distinction, and incident response.
- Generate `docs/BUILD_LOG.md` documenting failures and fixes.

#### Definition of done

The build is complete only when:

- Every Version 2 deterministic service and all nine agents exist and are wired.
- Every strategy is capability-gated.
- The complete paper lifecycle runs end to end.
- All risk and settlement bypass tests pass.
- Model outage cannot disable hard exits.
- Unsupported multi-leg strategies cannot reach execution.
- The same order intent cannot create duplicate broker orders.
- A stale quote cannot be approved.
- A broker/account mismatch halts new entries.
- The dashboard has no state-mutating path.
- Agent 9 and shadow testing are fully implemented.
- No TODO, placeholder, fake pass, or unimplemented production path remains.
- The final report clearly lists any true limitation that cannot be resolved without external access or unsupported broker functionality.

Work continuously through the dependency order. Do not ask for approval between normal engineering phases. Do not claim a component is complete unless its tests, type checks, lint checks, and runtime checks pass. When blocked by unavailable credentials or external services, implement the typed adapter, mocks, contract tests, and exact human setup instructions; never fabricate a successful live connection.

### END CLAUDE CODE PROMPT

---

## 23. Version 1 to Version 2 change summary

| Version 1 | Version 2 |
|---|---|
| Equities scanner | Options universe and chain scanner |
| Symbol-level signal | Complete strategy proposal |
| Share sizing | Maximum-loss and risk-budget sizing |
| Stock stop loss | Multi-dimensional options exit plan |
| ATR-focused risk | Greeks, max loss, DTE, IV, liquidity, and portfolio risk |
| Equity portfolio construction | Portfolio Greek and opportunity-cost allocation |
| Generic edge aggregator | Options Opportunity Engine |
| Equity research agents | Regime, catalyst, technical, volatility, strategy specialists |
| Agent-heavy calculations | Deterministic analytics services |
| Stock trade journal | Leg-, strategy-, Greek-, and fill-aware journal |
| General calibration | Strategy/DTE/delta/gamma/theta/IV calibration |
| Scheduled monitor | Session state machine plus event-driven risk monitoring |
| Generic dashboard | Options portfolio, Greeks, opportunity, and reconciliation dashboard |

---

## 24. Final operating requirements before live use

Before `ALLOW_LIVE_ORDERS=True` can be considered:

- Verify Robinhood account permissions and actual MCP capabilities.
- Confirm supported options strategies and native multi-leg behavior.
- Complete paper execution with realistic fills.
- Complete reconciliation and restart tests.
- Accumulate sufficient out-of-sample paper trades across more than one regime.
- Review expectancy after spreads and slippage.
- Confirm every hard guardrail with attempted-bypass tests.
- Confirm emergency halt and manual recovery.
- Review tax, assignment, exercise, and settlement implications.
- Start in approval-required mode with minimal risk.

The system's quality should be measured by disciplined rejection, reproducibility, calibrated uncertainty, controlled losses, execution quality, and portfolio survival - not by how frequently it trades.
