"""Structured JSON logging (CLAUDE.md §10 — never bare print)."""

import logging
import sys

import structlog


def configure(pretty: bool = False) -> None:
    renderer = structlog.dev.ConsoleRenderer() if pretty else structlog.processors.JSONRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
