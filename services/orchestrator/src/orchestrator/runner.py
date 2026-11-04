"""Event-driven glue that drives the Saga over RabbitMQ.

The orchestrator consumes:
  * provision.requested   -> fan out one ingestion.requested per document
  * ingestion.succeeded   -> mark doc done; when all done, register tools + activate
  * ingestion.failed      -> compensate (abort the saga)

It also runs a background *reaper* (``run_reaper``): every few seconds it fails
any saga that has made no progress for ``SAGA_TIMEOUT_SECONDS``. That is the
safety net for everything a message queue cannot promise: a lost message, a
worker that crashed mid-job, a poison message sitting in a dead-letter queue.

All DB mutations go through ``session_scope`` so each handler is one
transaction. On the terminal steps we publish provision.succeeded/failed so
other services (or the gateway) could react.
"""

from __future__ import annotations

import asyncio

from common.logging import get_logger
from common.messages import (
    IngestionFailed,
    IngestionRequested,
    IngestionSucceeded,
    ProvisionFailed,
    ProvisionRequested,
    ProvisionSucceeded,
    RoutingKey,
)
from common.messaging import Broker
from db.session import session_scope

from orchestrator import saga

log = get_logger(__name__)

QUEUE = "orchestrator"


class Orchestrator:
    def __init__(self, broker: Broker) -> None:
        self._broker = broker

    async def start(self) -> None:
        await self._broker.consume(
            QUEUE,
            [
                RoutingKey.PROVISION_REQUESTED,
                RoutingKey.INGESTION_SUCCEEDED,
                RoutingKey.INGESTION_FAILED,
            ],
            self.handle,
        )

    async def handle(self, message) -> None:
        if isinstance(message, ProvisionRequested):
            await self._on_provision_requested(message)
        elif isinstance(message, IngestionSucceeded):
            await self._on_ingestion_succeeded(message)
        elif isinstance(message, IngestionFailed):
            await self._on_ingestion_failed(message)
        else:  # pragma: no cover - defensive
            log.warning("orchestrator.unexpected_message", type=type(message).__name__)

    async def _on_provision_requested(self, msg: ProvisionRequested) -> None:
        log.info("orchestrator.provision_requested", saga_id=str(msg.saga_id))
        async with session_scope() as db:
            running = await saga.is_running(db, msg.saga_id)
            # No documents to ingest: skip straight to tools + activate.
            if running and not msg.document_ids:
                await saga.register_tools_and_activate(db, msg.saga_id)
        if not running:
            # Redelivered after the saga already finished, failed or was deleted.
            log.info("orchestrator.provision_ignored", saga_id=str(msg.saga_id))
            return
        if not msg.document_ids:
            await self._publish_success(msg)
            return
        # Fan out an ingestion job per document.
        for doc_id in msg.document_ids:
            await self._broker.publish(
                RoutingKey.INGESTION_REQUESTED,
                IngestionRequested(
                    correlation_id=msg.correlation_id,
                    saga_id=msg.saga_id,
                    agent_id=msg.agent_id,
                    document_id=doc_id,
                ),
            )

    async def _on_ingestion_succeeded(self, msg: IngestionSucceeded) -> None:
        async with session_scope() as db:
            became_ready = await saga.record_ingestion_success(
                db, msg.saga_id, msg.document_id
            )
        if became_ready:
            await self._publish_success(msg)

    async def _on_ingestion_failed(self, msg: IngestionFailed) -> None:
        async with session_scope() as db:
            compensated = await saga.compensate_if_running(
                db, msg.saga_id, reason=msg.reason
            )
        if compensated:
            await self._publish_failure(msg, msg.reason)

    # ----------------------------------------------------------------- reaper
    async def reap_stuck_sagas(self, timeout_seconds: int) -> int:
        """Fail every RUNNING saga with no progress for ``timeout_seconds``.

        Safe to run in several orchestrator replicas at once: each saga is
        locked and re-checked inside ``compensate_if_running``, so only one
        replica rolls it back.
        """
        async with session_scope() as db:
            stuck = await saga.find_stuck_sagas(db, timeout_seconds)

        reaped = 0
        for saga_id, agent_id in stuck:
            reason = f"timed out: no progress for {timeout_seconds}s"
            async with session_scope() as db:
                compensated = await saga.compensate_if_running(db, saga_id, reason)
            if not compensated:
                continue  # it finished (or another replica got it) in the meantime
            reaped += 1
            log.warning(
                "orchestrator.saga_timed_out",
                saga_id=str(saga_id),
                agent_id=str(agent_id),
                timeout_seconds=timeout_seconds,
            )
            await self._broker.publish(
                RoutingKey.PROVISION_FAILED,
                ProvisionFailed(
                    correlation_id=str(agent_id),
                    saga_id=saga_id,
                    agent_id=agent_id,
                    reason=reason,
                ),
            )
        return reaped

    async def run_reaper(self, interval_seconds: int, timeout_seconds: int) -> None:
        """Background loop started by the orchestrator's lifespan."""
        log.info(
            "orchestrator.reaper_started",
            interval_seconds=interval_seconds,
            timeout_seconds=timeout_seconds,
        )
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await self.reap_stuck_sagas(timeout_seconds)
            except Exception as exc:  # never let one bad pass kill the loop
                log.error("orchestrator.reaper_failed", error=str(exc))

    # ---------------------------------------------------------------- helpers
    async def _publish_success(self, msg) -> None:
        await self._broker.publish(
            RoutingKey.PROVISION_SUCCEEDED,
            ProvisionSucceeded(
                correlation_id=msg.correlation_id,
                saga_id=msg.saga_id,
                agent_id=msg.agent_id,
            ),
        )

    async def _publish_failure(self, msg, reason: str) -> None:
        await self._broker.publish(
            RoutingKey.PROVISION_FAILED,
            ProvisionFailed(
                correlation_id=msg.correlation_id,
                saga_id=msg.saga_id,
                agent_id=msg.agent_id,
                reason=reason,
            ),
        )
