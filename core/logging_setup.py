"""Structured logging so every agent call and decision is reconstructable after the fact.

Section 6 requires that "every agent call and every decision must log enough context
to fully reconstruct why the system did this." :func:`log_decision` emits a single
structured record (event name + arbitrary context) to the shared logger, which writes
to both console and ``logs/system.log``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from config import settings

_CONFIGURED = False


def configure_logging(level: int = logging.INFO) -> None:
    """Configure root logging once (idempotent). Console + rotating-free file handler."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_dir = settings.REPO_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    root = logging.getLogger()
    root.setLevel(level)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = logging.FileHandler(log_dir / "system.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a module logger, ensuring logging is configured."""
    configure_logging()
    return logging.getLogger(name)


def log_decision(logger: logging.Logger, event: str, **context: Any) -> None:
    """Log a structured decision record: an event name plus JSON-serialised context.

    Example: ``log_decision(log, "position_sized", symbol="AAPL", size_usd=42.0)``.
    """
    try:
        payload = json.dumps(context, default=str, sort_keys=True)
    except (TypeError, ValueError):
        payload = str(context)
    logger.info("%s | %s", event, payload)
