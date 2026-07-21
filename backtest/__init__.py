"""Backtest harness: drives the real 8-agent pipeline over historical daily bars.

Adds only a data source (historical bars) and a clock (walk-forward dates). It reuses the
unchanged agents, guardrails, sizing, and exit logic from the core system.
"""
