"""Agent runtime service: runs the LangGraph graph for a query.

Called by the gateway (not public). Loads the agent's tools, retrieves context
from pgvector, runs the multi-agent graph, and returns the answer + sources,
traced in LangFuse.
"""

from __future__ import annotations

import secrets
import uuid
from functools import partial
from typing import Literal

from common.llm import get_provider
from common.logging import bind_correlation_id, configure_logging, get_logger
from common.settings import get_settings
from db.models import Agent, AgentStatus, Tool
from db.session import get_sessionmaker
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_runtime.graph import run_agent
from agent_runtime.observability import build_callbacks
from agent_runtime.retrieval import retrieve

settings = get_settings()
settings.assert_production_safe()
configure_logging("agent-runtime", settings.log_level)
log = get_logger(__name__)
provider = get_provider(settings)

app = FastAPI(title="Agent Studio Runtime", version="1.0.0")


class Turn(BaseModel):
    """One earlier message of the conversation (sent by the gateway)."""

    role: Literal["user", "agent"]
    content: str = Field(max_length=4000)


class QueryIn(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    # The runtime keeps no conversation state: the gateway sends the last few turns.
    history: list[Turn] = Field(default_factory=list, max_length=20)


class Source(BaseModel):
    filename: str = ""
    chunk_index: int = 0
    snippet: str = ""
    score: float | None = None


class QueryOut(BaseModel):
    answer: str
    sources: list[Source] = Field(default_factory=list)


async def require_internal_key(
    x_internal_key: str | None = Header(default=None),
) -> None:
    """Only trusted services (the gateway) may call this API.

    The gateway authenticates users and checks agent ownership; this service
    does neither, so it must never be reachable without the shared key.
    Skipped when INTERNAL_API_KEY is empty (local development only).
    """
    expected = settings.internal_api_key
    if not expected:
        return
    if x_internal_key is None or not secrets.compare_digest(x_internal_key, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid internal key")


async def get_db():
    maker = get_sessionmaker()
    async with maker() as session:
        yield session


@app.middleware("http")
async def correlation_middleware(request: Request, call_next):
    cid = request.headers.get("x-correlation-id") or str(uuid.uuid4())
    bind_correlation_id(cid)
    response = await call_next(request)
    response.headers["x-correlation-id"] = cid
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/v1/agents/{agent_id}/query",
    response_model=QueryOut,
    dependencies=[Depends(require_internal_key)],
)
async def query(
    agent_id: uuid.UUID,
    body: QueryIn,
    db: AsyncSession = Depends(get_db),
) -> QueryOut:
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    if agent.status != AgentStatus.ACTIVE:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Agent is {agent.status.value}")

    tool_rows = await db.scalars(select(Tool.name).where(Tool.agent_id == agent_id))
    tool_names = list(tool_rows.all())

    # Bind a retrieval function that closes over this DB session + provider.
    retrieve_fn = partial(_retrieve_adapter, db)

    callbacks, trace_metadata = build_callbacks(
        session_id=str(agent_id), tags=["agent-query"]
    )

    result = await run_agent(
        agent_id=agent_id,
        question=body.question,
        provider=provider,
        retrieve_fn=retrieve_fn,
        tool_names=tool_names,
        history=[t.model_dump() for t in body.history],
        agent_name=agent.name,
        agent_description=agent.description,
        callbacks=callbacks,
        metadata=trace_metadata,
    )
    log.info("agent.query_answered", agent_id=str(agent_id))
    return QueryOut(**result)


async def _retrieve_adapter(db: AsyncSession, agent_id: str, question: str):
    return await retrieve(
        db,
        provider,
        uuid.UUID(agent_id),
        question,
        k=settings.retrieval_top_k,
        max_distance=settings.retrieval_max_distance,
    )
