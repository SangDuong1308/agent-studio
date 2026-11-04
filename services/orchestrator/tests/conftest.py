"""SQLite-backed fixtures for Saga unit tests (no Postgres, no RabbitMQ)."""

from __future__ import annotations

import os

os.environ.setdefault("LLM_PROVIDER", "fake")

import db.models as models
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

JSONB.compile = lambda self, *a, **k: "JSON"  # type: ignore
# SQLite can't create a pgvector column; swap it for JSON for these unit tests.
models.DocumentChunk.__table__.c.embedding.type = sa.JSON()


@pytest_asyncio.fixture
async def session_maker():
    # StaticPool: every session shares one in-memory database, so a test can seed
    # rows in one session and the code under test can read them in another.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        tables = [
            models.User.__table__,
            models.Agent.__table__,
            models.Document.__table__,
            models.DocumentChunk.__table__,
            models.Tool.__table__,
            models.SagaState.__table__,
        ]
        await conn.run_sync(lambda c: models.Base.metadata.create_all(c, tables=tables))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(session_maker):
    async with session_maker() as s:
        yield s
