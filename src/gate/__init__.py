"""Phase F — the deterministic trade gate and risk system (spec Section 3.1).

Everything in this package is pure code: the ten-step guardrail precedence, the
kill-switch panel with its monotonic halt epoch, committee aggregation (agents
may only reduce the code-computed risk budget, never increase it), and the
approval token that only the gate can mint. No LLM output is ever the final
authority over a capital-at-risk action here — agent decisions arrive as
validated inputs and can only terminate or shrink a trade.
"""
