"""Phase I — learning, calibration, and model risk (spec Section 13).

Fully implemented, never stubbed. Everything here is deterministic
measurement and process code: buckets over the Section 13.2 dimensions,
Section 13.3 metrics, and the Section 13.4 promotion lifecycle. Two rules
are enforced in code, not prose: a loss is not automatically an error (hard
minimum-sample gates — no tuning from individual outcomes), and no agent can
alter hard guardrails (the schemas forbid it and this layer refuses again).
Shadow configs evaluate data and produce decisions on paper only — this
package has no import path to the gate, the submitter, or any broker.
"""
