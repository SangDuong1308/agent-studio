"""SQLite fixtures. SQLite lacks pgvector, so we store the embedding column as
JSON for these unit tests - we're asserting chunk counts and idempotency, not
vector math (that's covered in the db integration test against Postgres)."""

from __future__ import annotations

import os

os.environ.setdefault("LLM_PROVIDER", "fake")

import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import db.models as models

JSONB.compile = lambda self, *a, **k: "JSON"  # type: ignore

# Swap the Vector column type for JSON on SQLite.
models.DocumentChunk.__table__.c.embedding.type = sa.JSON()


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        tables = [
            models.User.__table__,
            models.Agent.__table__,
            models.Document.__table__,
            models.DocumentChunk.__table__,
        ]
        await conn.run_sync(lambda c: models.Base.metadata.create_all(c, tables=tables))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()
