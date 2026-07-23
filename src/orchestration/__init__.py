"""Phase H — session orchestration (spec Section 15).

A state machine, not a loose cron script: the session walks the exact
Section 15 states, startup validation gates the first scan and fails closed,
DEGRADED/HALTED are reachable from everywhere with explicitly restricted
recovery edges (HALTED only ever leaves via an identified human), and the
scheduler runs deterministic services on bounded cadences — hard-risk
monitoring ahead of research, LLM agents only on meaningful triggers or
scheduled windows, never on a busy loop.
"""
