"""Compare pipeline decisions under two data configs on the SAME historical data.

Two levels:
- SIGNAL level: probe every (symbol, day) under both configs and diff the aggregated direction,
  confidence, gate-crossing, and the fundamental sub-agent's own direction. This isolates the
  effect of the data change from any trade-path divergence.
- TRADE level: diff the set of executed trades from two full backtests.

Answers: "did the real fundamental signal actually change entries/exits, or not?"
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from agents.edge_aggregator import EdgeAggregator
from agents.research.base import ResearchAgent, ResearchContext
from agents.research.fundamental import FundamentalResearchAgent
from agents.research.macro import MacroResearchAgent
from agents.research.sentiment import SentimentResearchAgent
from agents.research.technical import TechnicalResearchAgent
from backtest.data import HistoricalBarStore
from backtest.variants import NeutralResearchAgent, RealFundamentalAgent
from core import market_data
from core.db import Database
from core.llm import ModelClient, OfflineProvider
from core.market_data import get_market_regime


@dataclass
class ProbeRow:
    symbol: str
    date: str
    agg_direction: str
    agg_conf: float
    fund_direction: str
    tradeable: bool  # long AND agg_conf >= 0.65 (a cash-account entry candidate)


def _agents(client: object, fundamental_mode: str, sentiment_mode: str,
            fund_store: object | None) -> list[ResearchAgent]:
    tech = TechnicalResearchAgent(client, None)
    macro = MacroResearchAgent(client, None)
    fundamental: ResearchAgent
    if fundamental_mode == "real" and fund_store is not None:
        fundamental = RealFundamentalAgent(client, None, fund_store)
    elif fundamental_mode == "neutral":
        fundamental = NeutralResearchAgent(client, None, "research_fundamental")
    else:
        fundamental = FundamentalResearchAgent(client, None)
    sentiment: ResearchAgent = (
        NeutralResearchAgent(client, None, "research_sentiment")
        if sentiment_mode == "neutral" else SentimentResearchAgent(client, None)
    )
    return [tech, fundamental, sentiment, macro]


def probe_signals(store: HistoricalBarStore, universe: list[str], fundamental_mode: str,
                  sentiment_mode: str, warmup: int = 25) -> list[ProbeRow]:
    client = ModelClient(provider=OfflineProvider())
    fund_store: object | None = None
    if fundamental_mode == "real":
        from backtest.fundamentals import FundamentalsStore
        fund_store = FundamentalsStore.fetch(universe)
    agents = _agents(client, fundamental_mode, sentiment_mode, fund_store)
    aggregator = EdgeAggregator(client, None)

    market_data.set_snapshot_provider(store.provider)  # so get_market_regime is point-in-time
    rows: list[ProbeRow] = []
    try:
        for date in store.trading_dates(warmup):
            store.set_current_date(date)
            regime = get_market_regime(store.calendar_symbol)
            for sym in universe:
                if sym == store.calendar_symbol:
                    continue
                snap = store.snapshot_asof(sym, date)
                if snap is None:
                    continue
                ctx = ResearchContext(sym, snap, regime)
                sigs = [a.analyze(ctx) for a in agents]
                agg = aggregator.aggregate(sym, sigs, snap, regime)
                fund = next(s for s in sigs if s.source_agent == "research_fundamental")
                rows.append(ProbeRow(
                    symbol=sym, date=pd.Timestamp(date).date().isoformat(),
                    agg_direction=agg.direction, agg_conf=round(agg.calibrated_confidence, 4),
                    fund_direction=fund.direction,
                    tradeable=(agg.direction == "long" and agg.calibrated_confidence >= 0.65),
                ))
    finally:
        market_data.clear_snapshot_provider()
    return rows


def compare_probes(base: list[ProbeRow], variant: list[ProbeRow]) -> dict[str, object]:
    bi = {(r.symbol, r.date): r for r in base}
    vi = {(r.symbol, r.date): r for r in variant}
    keys = sorted(bi.keys() & vi.keys())
    n = len(keys)
    agg_dir_changes = sum(1 for k in keys if bi[k].agg_direction != vi[k].agg_direction)
    fund_dir_diffs = sum(1 for k in keys if bi[k].fund_direction != vi[k].fund_direction)
    gate_flips = sum(1 for k in keys if bi[k].tradeable != vi[k].tradeable)
    conf_deltas = [abs(bi[k].agg_conf - vi[k].agg_conf) for k in keys]
    real_had_signal = sum(1 for r in variant if r.fund_direction != "flat")
    flip_examples = [
        {"symbol": k[0], "date": k[1], "base_tradeable": bi[k].tradeable,
         "variant_tradeable": vi[k].tradeable,
         "base_conf": bi[k].agg_conf, "variant_conf": vi[k].agg_conf}
        for k in keys if bi[k].tradeable != vi[k].tradeable
    ][:12]
    return {
        "symbol_days": n,
        "fundamental_direction_differs": fund_dir_diffs,
        "fundamental_had_real_signal": real_had_signal,
        "aggregated_direction_changed": agg_dir_changes,
        "gate_crossing_flips": gate_flips,
        "mean_abs_conf_delta": round(sum(conf_deltas) / n, 4) if n else 0.0,
        "max_abs_conf_delta": round(max(conf_deltas), 4) if conf_deltas else 0.0,
        "flip_examples": flip_examples,
    }


def trade_signatures(db: Database) -> set[tuple[str, str, str, str]]:
    out: set[tuple[str, str, str, str]] = set()
    for t in db.all_trades():
        out.add((
            str(t["symbol"]), str(t.get("entry_ts") or "")[:10],
            str(t.get("exit_ts") or "OPEN")[:10], str(t.get("exit_reason") or "open"),
        ))
    return out


def format_comparison(sig: dict[str, object], base_db: Database, variant_db: Database) -> str:
    bt = trade_signatures(base_db)
    vt = trade_signatures(variant_db)
    only_base = sorted(bt - vt)
    only_variant = sorted(vt - bt)
    lines: list[str] = []
    lines.append("=" * 68)
    lines.append("DECISION COMPARISON — offline-noise fundamentals  vs  REAL fundamentals")
    lines.append("=" * 68)
    lines.append("SIGNAL LEVEL (same symbol x day grid under both configs):")
    lines.append(f"  symbol-days compared             {sig['symbol_days']}")
    lines.append(f"  days the fundamental agent had a REAL (non-flat) signal   "
                 f"{sig['fundamental_had_real_signal']}")
    lines.append(f"  fundamental direction differed   {sig['fundamental_direction_differs']}")
    lines.append(f"  aggregated direction changed     {sig['aggregated_direction_changed']}")
    lines.append(f"  0.65 gate-crossing flips         {sig['gate_crossing_flips']}")
    lines.append(f"  mean |Δ confidence|              {sig['mean_abs_conf_delta']}")
    lines.append(f"  max  |Δ confidence|              {sig['max_abs_conf_delta']}")
    flips = sig["flip_examples"]
    if isinstance(flips, list) and flips:
        lines.append("  gate flips (base->variant tradeable):")
        for f in flips:
            lines.append(f"    {f['symbol']:6s} {f['date']}  {f['base_tradeable']!s:5s} "
                         f"(conf {f['base_conf']}) -> {f['variant_tradeable']!s:5s} "
                         f"(conf {f['variant_conf']})")
    lines.append("")
    lines.append("TRADE LEVEL (executed trades from the two full backtests):")
    lines.append(f"  trades baseline={len(bt)}  variant={len(vt)}  common={len(bt & vt)}")
    lines.append(f"  only in baseline: {only_base if only_base else 'none'}")
    lines.append(f"  only in variant:  {only_variant if only_variant else 'none'}")
    return "\n".join(lines)
