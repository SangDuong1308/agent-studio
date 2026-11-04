"""Test fixtures: an in-memory SQLite DB and an ASGI test client.

We override the DB dependency and the broker so gateway tests run fully offline
with no Postgres or RabbitMQ. SQLite can't do pgvector, so the embedding column is
swapped for plain JSON: enough for rows to exist (and for deleting an agent to
cascade), but similarity search is not tested here.
"""

from __future__ import annotations

import os

os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("JWT_SECRET", "test-secret")
# Tests register/login many times from one address; limits are not under test.
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

import db.models as models
import pytest_asyncio
import sqlalchemy as sa
from gateway import broker as broker_mod
from gateway.deps import get_db
from gateway.main import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects.postgresql import JSONB

# Make Postgres-specific types work on SQLite for CRUD tests.
from sqlalchemy.dialects.sqlite import dialect as _sqlite  # noqa: F401
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Compile JSONB as TEXT-ish on sqlite.
JSONB.compile = lambda self, *a, **k: "JSON"  # type: ignore
# SQLite can't create a pgvector column; use JSON so the table can exist.
models.DocumentChunk.__table__.c.embedding.type = sa.JSON()


class _FakeBroker:
    def __init__(self) -> None:
        self.published: list = []

    async def connect(self) -> None:  # pragma: no cover - trivial
        pass

    async def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def publish(self, routing_key, message) -> None:
        self.published.append((routing_key, message))


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        tables = [
            models.User.__table__,
            models.Agent.__table__,
            models.Document.__table__,
            models.DocumentChunk.__table__,
            models.Tool.__table__,
            models.SagaState.__table__,
            models.ChatMessage.__table__,
        ]
        await conn.run_sync(lambda c: models.Base.metadata.create_all(c, tables=tables))
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def client(engine, monkeypatch):
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _get_db():
        async with maker() as session:
            yield session

    fake_broker = _FakeBroker()

    async def _get_broker():
        return fake_broker

    monkeypatch.setattr(broker_mod, "get_broker", _get_broker)
    # The app imported get_broker into its namespace; patch there too.
    import gateway.main as main_mod

    monkeypatch.setattr(main_mod, "get_broker", _get_broker)

    app.dependency_overrides[get_db] = _get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.fake_broker = fake_broker  # type: ignore[attr-defined]
        # Lets a test play the part of the orchestrator (flip an agent to ACTIVE)
        # and inspect rows directly.
        c.session_maker = maker  # type: ignore[attr-defined]
        yield c
    app.dependency_overrides.clear()
