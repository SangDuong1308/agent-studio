"""Ingestion worker entry point.

A long-running asyncio process that consumes ``ingestion.requested`` and emits
``ingestion.succeeded`` / ``ingestion.failed``. No HTTP surface - it's a pure
worker. Run with: ``python -m ingestion_worker.main``.
"""

from __future__ import annotations

import asyncio

from common.llm import get_provider
from common.logging import configure_logging, get_logger
from common.messages import (
    IngestionFailed,
    IngestionRequested,
    IngestionSucceeded,
    RoutingKey,
)
from common.messaging import Broker
from common.settings import get_settings
from db.session import session_scope
from ingestion_worker.ingest import ingest_document

QUEUE = "ingestion-worker"


async def run() -> None:
    settings = get_settings()
    configure_logging("ingestion-worker", settings.log_level)
    log = get_logger(__name__)

    provider = get_provider(settings)
    broker = Broker(settings.rabbitmq_url)
    await broker.connect()

    async def handle(message) -> None:
        assert isinstance(message, IngestionRequested)
        try:
            async with session_scope() as db:
                count = await ingest_document(db, provider, message.document_id)
            await broker.publish(
                RoutingKey.INGESTION_SUCCEEDED,
                IngestionSucceeded(
                    correlation_id=message.correlation_id,
                    saga_id=message.saga_id,
                    agent_id=message.agent_id,
                    document_id=message.document_id,
                    chunk_count=count,
                ),
            )
        except Exception as exc:
            log.error("ingest.failed", document_id=str(message.document_id), error=str(exc))
            await broker.publish(
                RoutingKey.INGESTION_FAILED,
                IngestionFailed(
                    correlation_id=message.correlation_id,
                    saga_id=message.saga_id,
                    agent_id=message.agent_id,
                    document_id=message.document_id,
                    reason=str(exc),
                ),
            )

    await broker.consume(QUEUE, [RoutingKey.INGESTION_REQUESTED], handle)
    log.info("ingestion-worker.started")
    # Keep the process alive.
    await asyncio.Future()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
