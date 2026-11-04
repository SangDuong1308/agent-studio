"""Pydantic request/response models for the gateway API.

Keeping API schemas separate from DB models is deliberate: the wire contract can
evolve independently of the storage layer, and we never leak columns like
``hashed_password`` to clients.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from common.catalog import KNOWN_TOOL_NAMES
from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    email: EmailStr
    created_at: datetime


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


MAX_DOCUMENT_CHARS = 200_000
MAX_DOCUMENTS_PER_AGENT = 10


class DocumentIn(BaseModel):
    filename: str = Field(min_length=1, max_length=500)
    # Each 1,000 characters becomes a billable embedding call, so cap the size.
    content: str = Field(min_length=1, max_length=MAX_DOCUMENT_CHARS)


def _check_tool_names(names: list[str]) -> list[str]:
    unknown = sorted(set(names) - KNOWN_TOOL_NAMES)
    if unknown:
        raise ValueError(
            f"unknown tool(s): {', '.join(unknown)}. "
            f"Available: {', '.join(sorted(KNOWN_TOOL_NAMES))}"
        )
    # De-duplicate (keeps order): (agent_id, name) is unique in the database.
    return list(dict.fromkeys(names))


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    documents: list[DocumentIn] = Field(
        default_factory=list, max_length=MAX_DOCUMENTS_PER_AGENT
    )
    tool_names: list[str] = Field(default_factory=list)

    @field_validator("tool_names")
    @classmethod
    def _validate_tools(cls, names: list[str]) -> list[str]:
        return _check_tool_names(names)


class AgentUpdate(BaseModel):
    """Partial update: a field that is left out is left unchanged.

    ``tool_names`` is the *complete* new list (so ``[]`` removes every tool),
    whereas documents are expressed as changes: add these, remove those.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    tool_names: list[str] | None = None
    add_documents: list[DocumentIn] = Field(
        default_factory=list, max_length=MAX_DOCUMENTS_PER_AGENT
    )
    remove_document_ids: list[UUID] = Field(default_factory=list)

    @field_validator("tool_names")
    @classmethod
    def _validate_tools(cls, names: list[str] | None) -> list[str] | None:
        return None if names is None else _check_tool_names(names)

    @model_validator(mode="after")
    def _something_changes(self) -> AgentUpdate:
        if (
            self.name is None
            and self.description is None
            and self.tool_names is None
            and not self.add_documents
            and not self.remove_document_ids
        ):
            raise ValueError("nothing to update")
        return self


class AgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    description: str
    status: str
    created_at: datetime
    updated_at: datetime


class DocumentOut(BaseModel):
    """A document attached to an agent (the text itself is not returned)."""

    id: UUID
    filename: str
    characters: int
    ingested: bool
    created_at: datetime


class AgentDetail(AgentOut):
    """One agent with what the edit screen needs."""

    documents: list[DocumentOut] = Field(default_factory=list)
    tool_names: list[str] = Field(default_factory=list)
    # Outcome of the most recent saga. When the agent is ACTIVE but this says
    # FAILED, the last edit was rolled back and ``last_saga_error`` says why.
    last_saga_status: str | None = None
    last_saga_error: str = ""


class Page(BaseModel):
    """Generic pagination envelope used by list endpoints."""

    items: list[AgentOut]
    total: int
    limit: int
    offset: int


class QueryIn(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


class Source(BaseModel):
    """A document passage the answer was grounded in."""

    filename: str = ""
    chunk_index: int = 0
    snippet: str = ""
    score: float | None = None


class QueryOut(BaseModel):
    answer: str
    sources: list[Source] = Field(default_factory=list)


class MessageOut(BaseModel):
    """One stored chat message (what the chat screen reloads)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    role: str  # "user" or "agent"
    content: str
    sources: list[Source] = Field(default_factory=list)
    created_at: datetime
