"""Saga tests: happy path, partial ingestion, and compensation on failure."""

from __future__ import annotations

from uuid import uuid4

import pytest

from db.models import Agent, AgentStatus, SagaState, SagaStatus, Tool, User
from orchestrator import saga


async def _seed(session, doc_ids: list[str], tool_names: list[str]):
    user = User(email=f"{uuid4()}@x.com", hashed_password="x")
    session.add(user)
    await session.flush()
    agent = Agent(owner_id=user.id, name="a", status=AgentStatus.PROVISIONING)
    session.add(agent)
    await session.flush()
    for name in tool_names:
        session.add(Tool(agent_id=agent.id, name=name))
    s = SagaState(
        agent_id=agent.id,
        status=SagaStatus.RUNNING,
        pending_documents=doc_ids,
        payload={"tool_names": tool_names},
    )
    session.add(s)
    await session.flush()
    return agent, s


@pytest.mark.asyncio
async def test_happy_path_single_doc(session):
    doc_id = str(uuid4())
    agent, s = await _seed(session, [doc_id], ["calculator"])

    all_done = await saga.mark_document_ingested(session, s.id, doc_id)
    assert all_done is True

    await saga.register_tools_and_activate(session, s.id)
    await session.refresh(agent)
    await session.refresh(s)

    assert agent.status == AgentStatus.ACTIVE
    assert s.status == SagaStatus.COMPLETED
    assert saga.STEP_ACTIVATE in s.completed_steps


@pytest.mark.asyncio
async def test_waits_for_all_documents(session):
    d1, d2 = str(uuid4()), str(uuid4())
    agent, s = await _seed(session, [d1, d2], [])

    assert await saga.mark_document_ingested(session, s.id, d1) is False
    assert await saga.mark_document_ingested(session, s.id, d2) is True


@pytest.mark.asyncio
async def test_compensation_on_ingestion_failure(session):
    doc_id = str(uuid4())
    agent, s = await _seed(session, [doc_id], ["calculator"])

    await saga.compensate(session, s.id, reason="embedding provider down")
    await session.refresh(agent)
    await session.refresh(s)

    assert agent.status == AgentStatus.FAILED
    assert s.status == SagaStatus.FAILED
    assert s.error == "embedding provider down"
    # Tools were removed as part of compensation.
    remaining = await session.scalar(
        __import__("sqlalchemy").select(__import__("sqlalchemy").func.count())
        .select_from(Tool)
        .where(Tool.agent_id == agent.id)
    )
    assert remaining == 0


@pytest.mark.asyncio
async def test_compensation_after_tools_registered(session):
    doc_id = str(uuid4())
    agent, s = await _seed(session, [doc_id], ["calculator"])
    await saga.mark_document_ingested(session, s.id, doc_id)
    await saga.register_tools_and_activate(session, s.id)
    # Now simulate a late failure and compensate.
    await saga.compensate(session, s.id, reason="late failure")
    await session.refresh(agent)
    assert agent.status == AgentStatus.FAILED
