"""Cost estimation for running the backtest on a REAL model — without calling any API.

Every agent builds its real system+user prompt and passes it to ``ModelClient.complete_json``
even on the offline path, so input tokens are measured from the actual prompts that WOULD be
sent. Output tokens can't be measured offline (no real generation), so they are estimated from
a documented per-call assumption band. No network / API call is made.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from core.llm import ModelClient, OfflineProvider

# Rough English heuristic: ~4 characters per token.
CHARS_PER_TOKEN = 4.0

# Published rates (USD per 1M tokens) as (input, output).
RATES: dict[str, tuple[float, float]] = {
    "Fable 5": (10.0, 50.0),
    "Sonnet 5": (2.0, 10.0),
    "Haiku 4.5": (0.80, 4.0),
}

# Output-token-per-call assumptions (these responses are short structured JSON + a sentence).
OUTPUT_LOW = 75
OUTPUT_MID = 150
OUTPUT_HIGH = 350


def est_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


@dataclass
class CostMeter:
    calls: int = 0
    input_tokens: int = 0
    schema_output_tokens: int = 0  # tokens in the offline_result JSON (a low-bound proxy)
    per_agent_calls: dict[str, int] = field(default_factory=dict)
    per_agent_input: dict[str, int] = field(default_factory=dict)

    def record(self, agent_key: str, system: str, user: str, offline_result: object) -> None:
        it = est_tokens(system) + est_tokens(user)
        self.calls += 1
        self.input_tokens += it
        self.schema_output_tokens += est_tokens(json.dumps(offline_result))
        self.per_agent_calls[agent_key] = self.per_agent_calls.get(agent_key, 0) + 1
        self.per_agent_input[agent_key] = self.per_agent_input.get(agent_key, 0) + it

    def avg_input_per_call(self) -> float:
        return self.input_tokens / self.calls if self.calls else 0.0


class MeteringModelClient(ModelClient):
    """A ModelClient that records prompt sizes/counts, then returns the offline result.

    Makes NO API call — it delegates to the offline path after recording.
    """

    def __init__(self, meter: CostMeter) -> None:
        super().__init__(provider=OfflineProvider(), db=None)
        self.meter = meter

    def complete_json(  # type: ignore[override]
        self, agent_key: str, system: str, user: str, offline_result: dict[str, object],
        *, agent_name: str | None = None,
    ) -> object:
        self.meter.record(agent_key, system, user, offline_result)
        return super().complete_json(agent_key, system, user, offline_result, agent_name=agent_name)


def dollars(input_tokens: int, output_tokens: int, rate_in: float, rate_out: float) -> float:
    return input_tokens / 1_000_000 * rate_in + output_tokens / 1_000_000 * rate_out


def format_estimate(label: str, meter: CostMeter) -> str:
    lines: list[str] = []
    lines.append("=" * 68)
    lines.append(f"COST ESTIMATE — {label}")
    lines.append("=" * 68)
    lines.append(f"model calls          {meter.calls:,}")
    lines.append(f"input tokens (real)  {meter.input_tokens:,}  (avg {meter.avg_input_per_call():.0f}/call)")
    lines.append(f"output tokens (est)  {OUTPUT_MID}/call assumed  "
                 f"(band {OUTPUT_LOW}-{OUTPUT_HIGH}); schema-min ~"
                 f"{meter.schema_output_tokens // max(meter.calls,1)}/call")
    lines.append("")
    lines.append("calls by agent (drivers first):")
    for agent, n in sorted(meter.per_agent_calls.items(), key=lambda kv: -kv[1]):
        share = 100 * n / meter.calls if meter.calls else 0
        lines.append(f"    {agent:24s} {n:>10,}  ({share:4.1f}%)")
    lines.append("")

    out_mid = meter.calls * OUTPUT_MID
    lines.append(f"{'model':10s} {'input $':>10s} {'output $':>10s} {'TOTAL $ (mid)':>14s}"
                 f"   {'low..high':>16s}")
    for model, (ri, ro) in RATES.items():
        in_cost = meter.input_tokens / 1_000_000 * ri
        out_cost = out_mid / 1_000_000 * ro
        total = in_cost + out_cost
        lo = dollars(meter.input_tokens, meter.calls * OUTPUT_LOW, ri, ro)
        hi = dollars(meter.input_tokens, meter.calls * OUTPUT_HIGH, ri, ro)
        lines.append(f"{model:10s} {in_cost:>10.2f} {out_cost:>10.2f} {total:>14.2f}"
                     f"   {lo:>7.2f}..{hi:<7.2f}")
    return "\n".join(lines)
