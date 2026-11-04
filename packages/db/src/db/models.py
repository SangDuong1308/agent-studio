"""SQLAlchemy 2.0 models.

Schema design notes:
  * UUID primary keys -> safe to expose in URLs, no sequential enumeration.
  * ``status`` columns are indexed because list endpoints filter on them.
  * ``DocumentChunk.embedding`` is a pgvector column; migration 0002 gives it an
    HNSW index for approximate nearest-neighbour search (see DECISIONS.md).
  * ``SagaState`` persists the orchestration state so a crashed orchestrator can
    resume / compensate. ``completed_steps`` records what to undo on failure.
  * Foreign keys use ON DELETE CASCADE so deleting an agent cleans up its
    documents, chunks, and tools.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from common.llm import EMBEDDING_DIM


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class AgentStatus(str, enum.Enum):
    PROVISIONING = "PROVISIONING"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"


class SagaStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    COMPENSATING = "COMPENSATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    agents: Mapped[list["Agent"]] = relationship(back_populates="owner")


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[AgentStatus] = mapped_column(
        Enum(AgentStatus, name="agent_status"),
        default=AgentStatus.PROVISIONING,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    owner: Mapped[User] = relationship(back_populates="agents")
    documents: Mapped[list["Document"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan"
    )
    tools: Mapped[list["Tool"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan"
    )
    messages: Mapped[list[ChatMessage]] = relationship(
        back_populates="agent", cascade="all, delete-orphan"
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    agent_id: Mapped[UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text)
    ingested: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    agent: Mapped[Agent] = relationship(back_populates="documents")
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))

    document: Mapped[Document] = relationship(back_populates="chunks")


class Tool(Base):
    __tablename__ = "tools"
    __table_args__ = (UniqueConstraint("agent_id", "name", name="uq_tool_agent_name"),)

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    agent_id: Mapped[UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    config: Mapped[dict] = mapped_column(JSONB, default=dict)

    agent: Mapped[Agent] = relationship(back_populates="tools")


class ChatMessage(Base):
    """One message of an agent's conversation.

    Each agent has a single conversation thread (only its owner can chat with it),
    so a message needs only ``agent_id``. ``role`` is "user" or "agent". Agent
    messages keep the retrieved passages in ``sources`` so a reloaded chat looks
    exactly like the live one.
    """

    __tablename__ = "chat_messages"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    agent_id: Mapped[UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(10))
    content: Mapped[str] = mapped_column(Text)
    sources: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )

    agent: Mapped[Agent] = relationship(back_populates="messages")


class SagaState(Base):
    """Persisted state of one provisioning saga (orchestration-based)."""

    __tablename__ = "saga_state"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    agent_id: Mapped[UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[SagaStatus] = mapped_column(
        Enum(SagaStatus, name="saga_status"),
        default=SagaStatus.RUNNING,
        index=True,
    )
    current_step: Mapped[str] = mapped_column(String(100), default="")
    completed_steps: Mapped[list] = mapped_column(JSONB, default=list)
    # Documents we are still waiting to finish ingestion for.
    pending_documents: Mapped[list] = mapped_column(JSONB, default=list)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
