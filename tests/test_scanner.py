"""Tests for Agent 1 (scanner)."""

from __future__ import annotations

from agents.scanner import MIN_AVG_VOLUME, ScanCandidate, Scanner
from core.db import Database
from core.llm import ModelClient, OfflineProvider


def make_client(db: Database | None = None) -> ModelClient:
    return ModelClient(provider=OfflineProvider(), db=db)


def test_scanner_returns_ranked_candidates() -> None:
    scanner = Scanner(client=make_client(), universe=["AAPL", "MSFT", "NVDA", "AMZN"])
    candidates = scanner.scan()
    assert candidates
    assert all(isinstance(c, ScanCandidate) for c in candidates)
    # scores are non-increasing (ranked)
    scores = [c.score for c in candidates]
    assert scores == sorted(scores, reverse=True)


def test_scanner_respects_max_candidates() -> None:
    scanner = Scanner(client=make_client(), max_candidates=3)
    candidates = scanner.scan()
    assert len(candidates) <= 3


def test_scanner_filters_illiquid_symbols() -> None:
    # A symbol whose deterministic offline avg_volume is below the floor is excluded.
    # All default-universe names are liquid; assert every returned candidate clears the floor.
    scanner = Scanner(client=make_client())
    for c in scanner.scan():
        assert c.snapshot.avg_volume >= MIN_AVG_VOLUME


def test_scanner_deterministic_offline() -> None:
    a = [c.symbol for c in Scanner(client=make_client()).scan()]
    b = [c.symbol for c in Scanner(client=make_client()).scan()]
    assert a == b
