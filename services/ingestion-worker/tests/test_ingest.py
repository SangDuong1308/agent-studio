"""Ingestion tests: chunking, embedding, upsert, idempotency."""

from __future__ import annotations

from uuid import uuid4

import pytest
import sqlalchemy as sa

from common.llm import FakeProvider
from db.models import Agent, AgentStatus, Document, DocumentChunk, User
from ingestion_worker.chunking import chunk_text
from ingestion_worker.ingest import ingest_document


def test_chunk_short_text_single_chunk():
    assert chunk_text("hello") == ["hello"]


def test_chunk_empty_text():
    assert chunk_text("   ") == []


def test_chunk_long_text_overlapping():
    text = "a" * 1200
    chunks = chunk_text(text, chunk_size=500, overlap=50)
    assert len(chunks) >= 2
    assert all(len(c) <= 500 for c in chunks)


async def _seed_document(session, content: str) -> Document:
    user = User(email=f"{uuid4()}@x.com", hashed_password="x")
    session.add(user)
    await session.flush()
    agent = Agent(owner_id=user.id, name="a", status=AgentStatus.PROVISIONING)
    session.add(agent)
    await session.flush()
    doc = Document(agent_id=agent.id, filename="f.txt", content=content)
    session.add(doc)
    await session.flush()
    return doc


@pytest.mark.asyncio
async def test_ingest_creates_chunks(session):
    doc = await _seed_document(session, "x" * 1200)
    provider = FakeProvider()
    count = await ingest_document(session, provider, doc.id)
    assert count >= 2
    stored = await session.scalar(
        sa.select(sa.func.count()).select_from(DocumentChunk).where(
            DocumentChunk.document_id == doc.id
        )
    )
    assert stored == count
    await session.refresh(doc)
    assert doc.ingested is True


@pytest.mark.asyncio
async def test_ingest_is_idempotent(session):
    doc = await _seed_document(session, "y" * 1200)
    provider = FakeProvider()
    first = await ingest_document(session, provider, doc.id)
    second = await ingest_document(session, provider, doc.id)
    assert first == second
    stored = await session.scalar(
        sa.select(sa.func.count()).select_from(DocumentChunk).where(
            DocumentChunk.document_id == doc.id
        )
    )
    # Re-running does not duplicate chunks.
    assert stored == first


@pytest.mark.asyncio
async def test_embedding_dimension(session):
    doc = await _seed_document(session, "hello world")
    provider = FakeProvider()
    vectors = await provider.embed(["hello world"])
    assert len(vectors[0]) == 768
