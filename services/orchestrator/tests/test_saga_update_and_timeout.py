"""Saga safety rules: update-mode compensation, status guards, late events, timeout.

These cover what makes the saga trustworthy when RabbitMQ delivers messages late,
twice, or never:
  * a failed *edit* removes only what the edit added and keeps the agent working
  * only a RUNNING saga reacts to events (late / duplicate ones are ignored)
  * a saga that stops making progress is failed by the reaper
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from common.messages import IngestionSucceeded, ProvisionRequested, RoutingKey
from db.models import (
    Agent,
    AgentStatus,
    Document,
    DocumentChunk,
    SagaState,
    SagaStatus,
    Tool,
    User,
)
from orchestrator import runner as runner_mod
from orchestrator import saga
from orchestrator.runner import Orchestrator
from sqlalchemy import func, select


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
async def _count(session, model, **where) -> int:
    stmt = select(func.count()).select_from(model)
    for col, value in where.items():
        stmt = stmt.where(getattr(model, col) == value)
    return await session.scalar(stmt)


async def _add_doc(session, agent, filename, with_chunk=True) -> Document:
    doc = Document(agent_id=agent.id, filename=filename, content="text", ingested=True)
    session.add(doc)
    await session.flush()
    if with_chunk:
        session.add(
            DocumentChunk(
                document_id=doc.id,
                agent_id=agent.id,
                chunk_index=0,
                content="chunk",
                embedding=[0.1, 0.2],
            )
        )
        await session.flush()
    return doc


async def _agent(session, status=AgentStatus.ACTIVE) -> Agent:
    user = User(email=f"{uuid4()}@x.com", hashed_password="x")
    session.add(user)
    await session.flush()
    agent = Agent(owner_id=user.id, name="bot", status=status)
    session.add(agent)
    await session.flush()
    return agent


async def _seed_update(session):
    """An ACTIVE agent (1 old document + chunk + 1 tool) that is mid-edit:
    a new document was added, the agent is PROVISIONING, an update saga runs."""
    agent = await _agent(session, AgentStatus.ACTIVE)
    old_doc = await _add_doc(session, agent, "old.txt")
    session.add(Tool(agent_id=agent.id, name="calculator"))
    new_doc = await _add_doc(session, agent, "new.txt", with_chunk=False)
    new_doc.ingested = False
    agent.status = AgentStatus.PROVISIONING
    s = SagaState(
        agent_id=agent.id,
        status=SagaStatus.RUNNING,
        pending_documents=[str(new_doc.id)],
        payload={
            "kind": "update",
            "previous_status": "ACTIVE",
            "new_document_ids": [str(new_doc.id)],
            "tool_names": ["calculator"],
        },
    )
    session.add(s)
    await session.flush()
    return agent, old_doc, new_doc, s


async def _seed_provision(session, status=SagaStatus.RUNNING):
    agent = await _agent(session, AgentStatus.PROVISIONING)
    doc = await _add_doc(session, agent, "faq.txt", with_chunk=False)
    session.add(Tool(agent_id=agent.id, name="calculator"))
    s = SagaState(
        agent_id=agent.id,
        status=status,
        pending_documents=[str(doc.id)],
        payload={"kind": "provision", "tool_names": ["calculator"]},
    )
    session.add(s)
    await session.flush()
    return agent, doc, s


# --------------------------------------------------------------------------- #
# update-mode saga
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_update_saga_success_reactivates_and_keeps_old_data(session):
    agent, old_doc, new_doc, s = await _seed_update(session)

    ready = await saga.record_ingestion_success(session, s.id, new_doc.id)

    assert ready is True
    await session.refresh(agent)
    await session.refresh(s)
    assert agent.status == AgentStatus.ACTIVE
    assert s.status == SagaStatus.COMPLETED
    assert await _count(session, DocumentChunk, document_id=old_doc.id) == 1


@pytest.mark.asyncio
async def test_failed_update_removes_only_the_new_document(session):
    agent, old_doc, new_doc, s = await _seed_update(session)
    # The worker managed to write a chunk for the new document before failing.
    session.add(
        DocumentChunk(
            document_id=new_doc.id, agent_id=agent.id, chunk_index=0,
            content="partial", embedding=[0.3, 0.4],
        )
    )
    await session.flush()

    await saga.compensate(session, s.id, reason="embedding provider down")
    await session.refresh(agent)
    await session.refresh(s)

    # The agent goes back to what it was: working.
    assert agent.status == AgentStatus.ACTIVE
    assert s.status == SagaStatus.FAILED
    assert s.error == "embedding provider down"
    # New document and its chunks are gone ...
    assert await _count(session, Document, id=new_doc.id) == 0
    assert await _count(session, DocumentChunk, document_id=new_doc.id) == 0
    # ... but everything the agent had before is intact.
    assert await _count(session, Document, id=old_doc.id) == 1
    assert await _count(session, DocumentChunk, document_id=old_doc.id) == 1
    assert await _count(session, Tool, agent_id=agent.id) == 1


@pytest.mark.asyncio
async def test_failed_provision_still_rolls_back_the_whole_agent(session):
    agent, doc, s = await _seed_provision(session)

    await saga.compensate(session, s.id, reason="boom")
    await session.refresh(agent)

    assert agent.status == AgentStatus.FAILED
    assert await _count(session, Tool, agent_id=agent.id) == 0


# --------------------------------------------------------------------------- #
# only a RUNNING saga reacts
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_compensate_if_running_runs_once(session):
    agent, doc, s = await _seed_provision(session)

    assert await saga.compensate_if_running(session, s.id, "first") is True
    assert await saga.compensate_if_running(session, s.id, "second") is False
    await session.refresh(s)
    assert s.error == "first"  # the second call changed nothing


@pytest.mark.asyncio
async def test_stray_failure_event_cannot_wreck_a_completed_saga(session):
    agent, doc, s = await _seed_provision(session)
    await saga.record_ingestion_success(session, s.id, doc.id)  # completes it
    await session.refresh(agent)
    assert agent.status == AgentStatus.ACTIVE

    # A duplicate / late "ingestion.failed" arrives afterwards.
    assert await saga.compensate_if_running(session, s.id, "stale failure") is False
    await session.refresh(agent)
    assert agent.status == AgentStatus.ACTIVE
    assert await _count(session, Tool, agent_id=agent.id) == 1


@pytest.mark.asyncio
async def test_late_success_after_failure_deletes_orphan_chunks(session):
    agent, doc, s = await _seed_provision(session)
    await saga.compensate(session, s.id, reason="timed out")
    # The slow worker finishes AFTER the rollback and writes its chunk.
    session.add(
        DocumentChunk(
            document_id=doc.id, agent_id=agent.id, chunk_index=0,
            content="late", embedding=[0.5, 0.6],
        )
    )
    await session.flush()

    ready = await saga.record_ingestion_success(session, s.id, doc.id)

    assert ready is False
    await session.refresh(agent)
    assert agent.status == AgentStatus.FAILED  # not revived
    assert await _count(session, DocumentChunk, agent_id=agent.id) == 0  # no orphans


@pytest.mark.asyncio
async def test_duplicate_success_after_completion_keeps_chunks(session):
    agent, old_doc, new_doc, s = await _seed_update(session)
    await saga.record_ingestion_success(session, s.id, new_doc.id)
    session.add(
        DocumentChunk(
            document_id=new_doc.id, agent_id=agent.id, chunk_index=0,
            content="real", embedding=[0.7, 0.8],
        )
    )
    await session.flush()

    # RabbitMQ redelivers the same success event.
    assert await saga.record_ingestion_success(session, s.id, new_doc.id) is False
    assert await _count(session, DocumentChunk, document_id=new_doc.id) == 1


@pytest.mark.asyncio
async def test_event_for_a_deleted_saga_is_ignored(session):
    assert await saga.record_ingestion_success(session, uuid4(), uuid4()) is False
    assert await saga.compensate_if_running(session, uuid4(), "x") is False


# --------------------------------------------------------------------------- #
# timeout
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_find_stuck_sagas_only_returns_old_running_ones(session):
    _, _, stuck = await _seed_provision(session)
    _, _, fresh = await _seed_provision(session)
    _, _, done = await _seed_provision(session, status=SagaStatus.COMPLETED)
    old = datetime.now(UTC) - timedelta(minutes=10)
    stuck.updated_at = old
    done.updated_at = old  # old, but already finished: must not be touched
    await session.flush()

    found = await saga.find_stuck_sagas(session, timeout_seconds=300)

    assert [saga_id for saga_id, _ in found] == [stuck.id]
    assert fresh.id not in [saga_id for saga_id, _ in found]


@pytest.mark.asyncio
async def test_progress_resets_the_timeout_clock(session):
    agent, doc, s = await _seed_provision(session)
    d2 = await _add_doc(session, agent, "second.txt", with_chunk=False)
    s.pending_documents = [str(doc.id), str(d2.id)]
    s.updated_at = datetime.now(UTC) - timedelta(minutes=10)
    await session.flush()
    assert await saga.find_stuck_sagas(session, 300)  # stuck for now

    await saga.record_ingestion_success(session, s.id, doc.id)  # one doc arrives

    assert await saga.find_stuck_sagas(session, 300) == []  # clock restarted


class _FakeBroker:
    def __init__(self) -> None:
        self.published: list = []

    async def publish(self, routing_key, message) -> None:
        self.published.append((routing_key, message))


@pytest.fixture
def orchestrator(session_maker, monkeypatch):
    """An Orchestrator wired to the SQLite test database and a fake broker."""

    @asynccontextmanager
    async def _scope():
        async with session_maker() as s:
            yield s
            await s.commit()

    monkeypatch.setattr(runner_mod, "session_scope", _scope)
    broker = _FakeBroker()
    orch = Orchestrator(broker)  # type: ignore[arg-type]
    orch.broker = broker
    return orch


@pytest.mark.asyncio
async def test_reaper_fails_stuck_saga_and_announces_it(orchestrator, session_maker):
    async with session_maker() as s:
        agent, doc, saga_row = await _seed_provision(s)
        saga_row.updated_at = datetime.now(UTC) - timedelta(minutes=10)
        await s.commit()
        agent_id, saga_id = agent.id, saga_row.id

    reaped = await orchestrator.reap_stuck_sagas(timeout_seconds=300)

    assert reaped == 1
    async with session_maker() as s:
        agent = await s.get(Agent, agent_id)
        saga_row = await s.get(SagaState, saga_id)
        assert agent.status == AgentStatus.FAILED
        assert saga_row.status == SagaStatus.FAILED
        assert "timed out" in saga_row.error
    routing_key, message = orchestrator.broker.published[0]
    assert routing_key == RoutingKey.PROVISION_FAILED
    assert message.saga_id == saga_id

    # Running the reaper again finds nothing: it is idempotent.
    assert await orchestrator.reap_stuck_sagas(timeout_seconds=300) == 0


@pytest.mark.asyncio
async def test_reaper_returns_a_stuck_edit_to_active(orchestrator, session_maker):
    async with session_maker() as s:
        agent, old_doc, new_doc, saga_row = await _seed_update(s)
        saga_row.updated_at = datetime.now(UTC) - timedelta(minutes=10)
        await s.commit()
        agent_id, new_doc_id = agent.id, new_doc.id

    assert await orchestrator.reap_stuck_sagas(timeout_seconds=300) == 1

    async with session_maker() as s:
        assert (await s.get(Agent, agent_id)).status == AgentStatus.ACTIVE
        assert await s.get(Document, new_doc_id) is None


@pytest.mark.asyncio
async def test_reaper_leaves_a_busy_saga_alone(orchestrator, session_maker):
    async with session_maker() as s:
        await _seed_provision(s)  # updated just now
        await s.commit()

    assert await orchestrator.reap_stuck_sagas(timeout_seconds=300) == 0
    assert orchestrator.broker.published == []


# --------------------------------------------------------------------------- #
# runner: events for a saga that is no longer running
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_redelivered_provision_command_for_a_failed_saga_does_nothing(
    orchestrator, session_maker
):
    async with session_maker() as s:
        agent, doc, saga_row = await _seed_provision(s, status=SagaStatus.FAILED)
        await s.commit()
        msg = ProvisionRequested(
            correlation_id="c", saga_id=saga_row.id, agent_id=agent.id,
            document_ids=[doc.id],
        )

    await orchestrator.handle(msg)

    assert orchestrator.broker.published == []  # no ingestion jobs were fanned out


@pytest.mark.asyncio
async def test_runner_publishes_success_only_when_agent_becomes_ready(
    orchestrator, session_maker
):
    async with session_maker() as s:
        agent, doc, saga_row = await _seed_provision(s)
        await s.commit()
        done = IngestionSucceeded(
            correlation_id="c", saga_id=saga_row.id, agent_id=agent.id,
            document_id=doc.id, chunk_count=1,
        )

    await orchestrator.handle(done)
    published = [rk for rk, _ in orchestrator.broker.published]
    assert published == [RoutingKey.PROVISION_SUCCEEDED]

    await orchestrator.handle(done)  # duplicate delivery
    assert len(orchestrator.broker.published) == 1  # no second announcement
