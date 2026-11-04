"""Thin async RabbitMQ wrapper built on aio-pika.

Topology (declared idempotently on connect):

    exchange: "agent-studio"   (type=topic, durable)
    DLX:      "agent-studio.dlx" (type=topic, durable)

Consumers declare a durable queue bound to one or more routing-key patterns.
Each queue is created with a dead-letter exchange so that a message which
raises (or is rejected) is routed to the DLQ instead of being lost or looping
forever.

Design notes:
  * Topic exchange -> flexible routing with patterns (provision.*, ingestion.*).
  * Manual ack (ack after successful handling) -> at-least-once delivery.
  * Dead-letter queue -> poison messages are quarantined, not retried forever.
  * Pydantic validation on consume -> contract enforced at the boundary.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import aio_pika
from aio_pika.abc import AbstractIncomingMessage

from common.logging import bind_correlation_id, get_logger
from common.messages import MESSAGE_TYPES, Envelope, RoutingKey

log = get_logger(__name__)

EXCHANGE_NAME = "agent-studio"
DLX_NAME = "agent-studio.dlx"

Handler = Callable[[Envelope], Awaitable[None]]


class Broker:
    """Owns a connection + channel and the exchange topology."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._connection: aio_pika.abc.AbstractRobustConnection | None = None
        self._channel: aio_pika.abc.AbstractChannel | None = None
        self._exchange: aio_pika.abc.AbstractExchange | None = None
        self._dlx: aio_pika.abc.AbstractExchange | None = None

    async def connect(self) -> None:
        self._connection = await aio_pika.connect_robust(self._url)
        self._channel = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=10)
        self._dlx = await self._channel.declare_exchange(
            DLX_NAME, aio_pika.ExchangeType.TOPIC, durable=True
        )
        self._exchange = await self._channel.declare_exchange(
            EXCHANGE_NAME, aio_pika.ExchangeType.TOPIC, durable=True
        )
        log.info("broker.connected", url=self._url)

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()

    async def publish(self, routing_key: RoutingKey, message: Envelope) -> None:
        assert self._exchange is not None, "call connect() first"
        body = message.model_dump_json().encode()
        await self._exchange.publish(
            aio_pika.Message(
                body=body,
                content_type="application/json",
                correlation_id=message.correlation_id,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=routing_key.value,
        )
        log.info("message.published", routing_key=routing_key.value)

    async def consume(
        self,
        queue_name: str,
        routing_keys: list[RoutingKey],
        handler: Handler,
    ) -> None:
        """Declare a durable queue with a DLQ and start consuming forever."""
        assert self._channel is not None and self._exchange is not None

        # Dead-letter queue for this consumer.
        dlq = await self._channel.declare_queue(
            f"{queue_name}.dlq", durable=True
        )
        for rk in routing_keys:
            await dlq.bind(self._dlx, routing_key=rk.value)

        queue = await self._channel.declare_queue(
            queue_name,
            durable=True,
            arguments={"x-dead-letter-exchange": DLX_NAME},
        )
        for rk in routing_keys:
            await queue.bind(self._exchange, routing_key=rk.value)

        async def _on_message(raw: AbstractIncomingMessage) -> None:
            routing_key = raw.routing_key or ""
            try:
                payload: dict[str, Any] = json.loads(raw.body)
                cid = payload.get("correlation_id", "")
                if cid:
                    bind_correlation_id(cid)
                model_cls = MESSAGE_TYPES.get(routing_key)
                if model_cls is None:
                    raise ValueError(f"unknown routing key: {routing_key}")
                message = model_cls.model_validate(payload)
            except Exception as exc:  # malformed / unknown -> DLQ, no requeue
                log.error("message.invalid", routing_key=routing_key, error=str(exc))
                await raw.reject(requeue=False)
                return

            try:
                await handler(message)
                await raw.ack()
            except Exception as exc:  # handler failed -> DLQ, no infinite requeue
                log.error(
                    "message.handler_failed",
                    routing_key=routing_key,
                    error=str(exc),
                )
                await raw.reject(requeue=False)

        await queue.consume(_on_message)
        log.info("broker.consuming", queue=queue_name)
