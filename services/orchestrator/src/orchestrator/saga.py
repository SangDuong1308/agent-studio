"""The Saga (orchestration-based): how an agent gets built, or changed.

A Saga breaks one distributed transaction into a sequence of local steps, each
with a *compensating* action that undoes it. There is no distributed 2PC; if a
step fails we run the compensation for what this saga did and leave the system
in a consistent state.

There are two kinds of saga. They share all the machinery below; only the
compensation differs, because they have different things to undo:

  provision   creating an agent (gateway: POST /v1/agents)
      step 1  create agent + document rows     (done by the gateway)
      step 2  ingest documents                 comp: delete chunks + vectors
      step 3  register tools                   comp: unregister tools
      step 4  activate agent                   comp: (none - terminal success)
      On failure the whole agent is rolled back and marked FAILED.

  update      adding documents to an ACTIVE agent (gateway: PATCH /v1/agents/{id})
      step 1  create the new document rows     (done by the gateway)
      step 2  ingest only the NEW documents    comp: delete the new documents
      step 3  activate agent                   comp: (none)
      On failure only the new documents are removed and the agent goes back to
      its previous status (ACTIVE). It must keep working: one bad upload must
      not destroy an agent that was fine before.

Three safety rules make the saga trustworthy under RabbitMQ's at-least-once
delivery:

  1. Only a RUNNING saga reacts to events. A late or duplicated event for a saga
     that already finished (or already failed) is ignored. Without this, a
     stray "ingestion failed" message could wreck an agent that is ACTIVE.
  2. Every handler locks the saga row (SELECT ... FOR UPDATE) so two events for
     the same saga are processed one at a time.
  3. A saga that makes no progress for ``SAGA_TIMEOUT_SECONDS`` is failed and
     compensated by the reaper in ``runner.py`` (see ``find_stuck_sagas``).

This module takes a session and has no RabbitMQ in it, so it is unit-testable on
SQLite. The event-driven glue lives in ``runner.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from common.logging import get_logger
from db.models import (
    Agent,
    AgentStatus,
    Document,
    DocumentChunk,
    SagaState,
    SagaStatus,
    Tool,
)
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger(__name__)

KIND_PROVISION = "provision"
KIND_UPDATE = "update"

STEP_REGISTER_TOOLS = "register_tools"
STEP_ACTIVATE = "activate_agent"


def kind_of(saga: SagaState) -> str:
    """Sagas created before ``kind`` existed have no marker: they are provisions."""
    return (saga.payload or {}).get("kind", KIND_PROVISION)


async def _get_saga(db: AsyncSession, saga_id: UUID) -> SagaState:
    # SELECT ... FOR UPDATE: serialise concurrent handlers for the same saga.
    saga = await db.get(SagaState, saga_id, with_for_update=True)
    if saga is None:
        raise ValueError(f"saga {saga_id} not found")
    return saga


async def is_running(db: AsyncSession, saga_id: UUID) -> bool:
    saga = await db.get(SagaState, saga_id, with_for_update=True)
    return saga is not None and saga.status == SagaStatus.RUNNING


async def mark_document_ingested(
    db: AsyncSession, saga_id: UUID, document_id: UUID
) -> bool:
    """Record a document as ingested. Return True when ALL docs are done."""
    saga = await _get_saga(db, saga_id)
    # The row is locked (see _get_saga) so two documents finishing at the same
    # moment cannot overwrite each other's update to pending_documents.
    pending = [d for d in saga.pending_documents if d != str(document_id)]
    saga.pending_documents = pending
    saga.current_step = "ingesting"
    log.info("saga.document_ingested", saga_id=str(saga_id), remaining=len(pending))
    return len(pending) == 0


async def register_tools_and_activate(db: AsyncSession, saga_id: UUID) -> None:
    """Run the remaining steps after ingestion completes: tools + activate."""
    saga = await _get_saga(db, saga_id)

    if STEP_REGISTER_TOOLS not in saga.completed_steps:
        # Tools were already inserted by the gateway; here we validate/activate
        # them. In a real system this might call an external tool registry.
        tool_names = saga.payload.get("tool_names", [])
        registered = await db.scalars(
            select(Tool.name).where(Tool.agent_id == saga.agent_id)
        )
        log.info(
            "saga.tools_registered",
            saga_id=str(saga_id),
            tools=list(registered.all()) or tool_names,
        )
        saga.completed_steps = [*saga.completed_steps, STEP_REGISTER_TOOLS]

    if STEP_ACTIVATE not in saga.completed_steps:
        agent = await db.get(Agent, saga.agent_id)
        if agent is None:
            raise ValueError("agent disappeared mid-saga")
        agent.status = AgentStatus.ACTIVE
        saga.completed_steps = [*saga.completed_steps, STEP_ACTIVATE]
        saga.current_step = STEP_ACTIVATE
        saga.status = SagaStatus.COMPLETED
        log.info(
            "saga.completed",
            saga_id=str(saga_id),
            agent_id=str(saga.agent_id),
            kind=kind_of(saga),
        )

    await db.flush()


async def record_ingestion_success(
    db: AsyncSession, saga_id: UUID, document_id: UUID
) -> bool:
    """Handle an ``ingestion.succeeded`` event. Return True when the agent just
    became ready (every document ingested, saga completed).

    Rule 1 applies here: if the saga is no longer RUNNING the event is late.
      * saga FAILED  -> the worker finished after we already rolled back, so the
                        chunks it wrote are orphans: delete them.
      * saga COMPLETED -> a duplicate delivery: ignore, the chunks are valid.
    """
    saga = await db.get(SagaState, saga_id, with_for_update=True)
    if saga is None:
        log.warning("saga.event_for_missing_saga", saga_id=str(saga_id))
        return False
    if saga.status != SagaStatus.RUNNING:
        if saga.status == SagaStatus.FAILED:
            await db.execute(
                delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
            )
            log.warning(
                "saga.late_ingestion_discarded",
                saga_id=str(saga_id),
                document_id=str(document_id),
            )
        else:
            log.info("saga.duplicate_event_ignored", saga_id=str(saga_id))
        return False

    all_done = await mark_document_ingested(db, saga_id, document_id)
    if all_done:
        await register_tools_and_activate(db, saga_id)
    return all_done


async def compensate(db: AsyncSession, saga_id: UUID, reason: str) -> None:
    """Undo what this saga did and mark it FAILED.

    This is the heart of the Saga. It must be safe to run even if some steps
    never completed (compensations are no-ops when there's nothing to undo).
    Prefer ``compensate_if_running`` from event handlers: it applies rule 1.
    """
    saga = await _get_saga(db, saga_id)
    saga.status = SagaStatus.COMPENSATING
    saga.error = reason
    log.warning(
        "saga.compensating", saga_id=str(saga_id), kind=kind_of(saga), reason=reason
    )

    if kind_of(saga) == KIND_UPDATE:
        await _compensate_update(db, saga)
    else:
        await _compensate_provision(db, saga)

    saga.status = SagaStatus.FAILED
    saga.current_step = "compensated"
    await db.flush()
    log.warning("saga.failed", saga_id=str(saga_id), reason=reason)


async def _compensate_provision(db: AsyncSession, saga: SagaState) -> None:
    """The agent never became usable: remove everything and mark it FAILED."""
    # comp for step 3: unregister tools. The agent ends up FAILED, so we clear
    # every tool attached to it regardless of how far the saga got - leaving
    # orphaned tool rows on a dead agent would be inconsistent.
    await db.execute(delete(Tool).where(Tool.agent_id == saga.agent_id))
    log.info("saga.comp.tools_unregistered", saga_id=str(saga.id))

    # comp for step 2: delete any ingested chunks/vectors
    await db.execute(
        delete(DocumentChunk).where(DocumentChunk.agent_id == saga.agent_id)
    )
    log.info("saga.comp.vectors_deleted", saga_id=str(saga.id))

    # comp for step 1: mark the agent FAILED (terminal, visible to the user)
    agent = await db.get(Agent, saga.agent_id)
    if agent is not None:
        agent.status = AgentStatus.FAILED


async def _compensate_update(db: AsyncSession, saga: SagaState) -> None:
    """An edit failed: drop only the documents it added, keep the agent working."""
    new_ids = [UUID(i) for i in saga.payload.get("new_document_ids", [])]
    if new_ids:
        # Chunks first, then the documents. Explicit rather than relying on the
        # database's ON DELETE CASCADE, so it is obvious what gets undone.
        await db.execute(
            delete(DocumentChunk).where(DocumentChunk.document_id.in_(new_ids))
        )
        await db.execute(delete(Document).where(Document.id.in_(new_ids)))
        log.info(
            "saga.comp.new_documents_removed", saga_id=str(saga.id), count=len(new_ids)
        )

    agent = await db.get(Agent, saga.agent_id)
    if agent is not None:
        previous = saga.payload.get("previous_status", AgentStatus.ACTIVE.value)
        agent.status = AgentStatus(previous)


async def compensate_if_running(db: AsyncSession, saga_id: UUID, reason: str) -> bool:
    """Compensate only if the saga is still RUNNING (rule 1). Return True if it ran.

    Handlers for ``ingestion.failed`` and the timeout reaper both use this, so a
    saga that already completed is never rolled back by a stray message, and a
    saga that already failed is never compensated twice.
    """
    saga = await db.get(SagaState, saga_id, with_for_update=True)
    if saga is None or saga.status != SagaStatus.RUNNING:
        log.info("saga.compensation_skipped", saga_id=str(saga_id))
        return False
    await compensate(db, saga_id, reason)
    return True


async def find_stuck_sagas(
    db: AsyncSession, timeout_seconds: int
) -> list[tuple[UUID, UUID]]:
    """RUNNING sagas with no progress for ``timeout_seconds``: (saga_id, agent_id).

    ``updated_at`` moves every time the saga records progress, so this means
    "nothing has happened for N seconds", not "has been running for N seconds".
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=timeout_seconds)
    rows = await db.execute(
        select(SagaState.id, SagaState.agent_id).where(
            SagaState.status == SagaStatus.RUNNING,
            SagaState.updated_at < cutoff,
        )
    )
    return [(r.id, r.agent_id) for r in rows]
