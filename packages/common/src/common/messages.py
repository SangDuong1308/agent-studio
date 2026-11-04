"""Message contracts exchanged over RabbitMQ.

These Pydantic models are the *contract* between services. A publisher and a
consumer agree on the shape here; validation happens on both ends so a
malformed message is caught early (and routed to the dead-letter queue).

Routing keys follow ``<aggregate>.<event>`` so the orchestrator can bind to
patterns like ``provision.*`` or ``ingestion.*``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RoutingKey(str, Enum):
    # Commands (gateway -> orchestrator)
    PROVISION_REQUESTED = "provision.requested"
    # Commands (orchestrator -> ingestion-worker)
    INGESTION_REQUESTED = "ingestion.requested"
    # Events (ingestion-worker -> orchestrator)
    INGESTION_SUCCEEDED = "ingestion.succeeded"
    INGESTION_FAILED = "ingestion.failed"
    # Events (orchestrator -> anyone interested)
    PROVISION_SUCCEEDED = "provision.succeeded"
    PROVISION_FAILED = "provision.failed"


class Envelope(BaseModel):
    """Common metadata wrapped around every message."""

    message_id: UUID = Field(default_factory=uuid4)
    correlation_id: str
    occurred_at: datetime = Field(default_factory=_utcnow)


class ProvisionRequested(Envelope):
    saga_id: UUID
    agent_id: UUID
    document_ids: list[UUID] = Field(default_factory=list)
    tool_names: list[str] = Field(default_factory=list)


class IngestionRequested(Envelope):
    saga_id: UUID
    agent_id: UUID
    document_id: UUID


class IngestionSucceeded(Envelope):
    saga_id: UUID
    agent_id: UUID
    document_id: UUID
    chunk_count: int


class IngestionFailed(Envelope):
    saga_id: UUID
    agent_id: UUID
    document_id: UUID
    reason: str


class ProvisionSucceeded(Envelope):
    saga_id: UUID
    agent_id: UUID


class ProvisionFailed(Envelope):
    saga_id: UUID
    agent_id: UUID
    reason: str


# Map routing key -> model, so consumers can parse the right type.
MESSAGE_TYPES: dict[str, type[Envelope]] = {
    RoutingKey.PROVISION_REQUESTED: ProvisionRequested,
    RoutingKey.INGESTION_REQUESTED: IngestionRequested,
    RoutingKey.INGESTION_SUCCEEDED: IngestionSucceeded,
    RoutingKey.INGESTION_FAILED: IngestionFailed,
    RoutingKey.PROVISION_SUCCEEDED: ProvisionSucceeded,
    RoutingKey.PROVISION_FAILED: ProvisionFailed,
}
