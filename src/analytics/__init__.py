"""V2 deterministic analytics (spec Section 5).

Pure code, no LLM anywhere. Money is Decimal end-to-end; model-based estimates
(Black-Scholes Greeks, realized volatility) label themselves as estimates and record
their input assumptions. Missing or invalid inputs raise
:class:`src.domain.values.DomainValidationError` — nothing is ever guessed.
"""
