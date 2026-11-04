"""Structured JSON logging with correlation IDs.

A single user action fans out across gateway -> RabbitMQ -> orchestrator ->
ingestion-worker. To trace one action end to end, every log line carries a
``correlation_id`` that is propagated across service boundaries (HTTP headers
and message metadata).

Usage::

    from common.logging import configure_logging, get_logger, bind_correlation_id

    configure_logging("gateway")
    log = get_logger(__name__)
    bind_correlation_id("abc-123")
    log.info("agent.created", agent_id=str(agent_id))
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

import structlog

# Holds the correlation id for the current async task / request.
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def bind_correlation_id(correlation_id: str) -> None:
    _correlation_id.set(correlation_id)


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def _add_correlation_id(_logger, _method, event_dict):
    cid = _correlation_id.get()
    if cid is not None:
        event_dict["correlation_id"] = cid
    return event_dict


def configure_logging(service_name: str, level: str = "INFO") -> None:
    """Configure structlog to emit one JSON object per log line."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_correlation_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(service=service_name)


def get_logger(name: str = "app") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
