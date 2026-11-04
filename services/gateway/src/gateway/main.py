"""Gateway FastAPI application.

Responsibilities:
  * Auth (register, login -> JWT).
  * Agents CRUD with pagination and versioned (/v1) routes, including editing.
  * Kick off the Saga (create / edit-with-new-documents) by publishing a command
    to RabbitMQ.
  * Proxy agent queries to the agent-runtime service.

This is the only service exposed publicly; everything else sits behind it.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from common.logging import (
    bind_correlation_id,
    configure_logging,
    get_correlation_id,
    get_logger,
)
from common.messages import ProvisionRequested, RoutingKey
from common.settings import get_settings
from db.models import (
    Agent,
    AgentStatus,
    ChatMessage,
    Document,
    SagaState,
    SagaStatus,
    Tool,
    User,
)
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.broker import close_broker, get_broker
from gateway.deps import get_current_user, get_db
from gateway.ratelimit import client_ip, enforce
from gateway.schemas import (
    MAX_DOCUMENTS_PER_AGENT,
    AgentCreate,
    AgentDetail,
    AgentOut,
    AgentUpdate,
    DocumentOut,
    MessageOut,
    Page,
    QueryIn,
    QueryOut,
    Token,
    UserCreate,
    UserOut,
)
from gateway.security import create_access_token, hash_password, verify_password

settings = get_settings()
settings.assert_production_safe()
configure_logging("gateway", settings.log_level)
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Connect to RabbitMQ on startup so the first request is fast.
    try:
        await get_broker()
    except Exception as exc:  # don't crash if MQ is slow to come up locally
        log.warning("broker.connect_failed_on_startup", error=str(exc))
    yield
    await close_broker()


app = FastAPI(title="Agent Studio Gateway", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
    expose_headers=["X-Correlation-ID", "Retry-After"],
)


@app.middleware("http")
async def correlation_middleware(request: Request, call_next):
    """Attach a correlation id to every request for end-to-end tracing."""
    cid = request.headers.get("x-correlation-id") or str(uuid.uuid4())
    bind_correlation_id(cid)
    response = await call_next(request)
    response.headers["x-correlation-id"] = cid
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
@app.post(
    "/v1/auth/register", response_model=UserOut, status_code=status.HTTP_201_CREATED
)
async def register(
    body: UserCreate, request: Request, db: AsyncSession = Depends(get_db)
) -> User:
    enforce(f"auth:{client_ip(request)}", settings.rate_limit_auth_per_minute, 60)
    existing = await db.scalar(select(User).where(User.email == body.email))
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    # bcrypt is CPU-heavy (~100ms): keep it off the event loop.
    hashed = await run_in_threadpool(hash_password, body.password)
    user = User(email=body.email, hashed_password=hashed)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    log.info("user.registered", user_id=str(user.id))
    return user


@app.post("/v1/auth/login", response_model=Token)
async def login(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Token:
    enforce(f"auth:{client_ip(request)}", settings.rate_limit_auth_per_minute, 60)
    user = await db.scalar(select(User).where(User.email == form.username))
    if user is None or not await run_in_threadpool(
        verify_password, form.password, user.hashed_password
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    return Token(access_token=create_access_token(str(user.id)))


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #
@app.post("/v1/agents", response_model=AgentOut, status_code=status.HTTP_202_ACCEPTED)
async def create_agent(
    body: AgentCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Agent:
    """Create an agent (status=PROVISIONING) and start the provisioning saga.

    Returns 202 Accepted: provisioning happens asynchronously. Clients poll
    GET /v1/agents/{id} until status becomes ACTIVE or FAILED.
    """
    enforce(f"create:{user.id}", settings.rate_limit_create_per_hour, 3600)
    owned = await db.scalar(
        select(func.count()).select_from(Agent).where(Agent.owner_id == user.id)
    )
    if (owned or 0) >= settings.max_agents_per_user:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Agent limit reached ({settings.max_agents_per_user}). Delete one first.",
        )

    agent = Agent(
        owner_id=user.id,
        name=body.name,
        description=body.description,
        status=AgentStatus.PROVISIONING,
    )
    db.add(agent)
    await db.flush()  # get agent.id

    documents = [
        Document(agent_id=agent.id, filename=d.filename, content=d.content)
        for d in body.documents
    ]
    db.add_all(documents)
    for tool_name in body.tool_names:
        db.add(Tool(agent_id=agent.id, name=tool_name))
    await db.flush()

    saga = SagaState(
        agent_id=agent.id,
        status=SagaStatus.RUNNING,
        current_step="start",
        pending_documents=[str(d.id) for d in documents],
        payload={"kind": "provision", "tool_names": body.tool_names},
    )
    db.add(saga)
    await db.commit()
    await db.refresh(agent)

    await _publish_saga_start(db, agent, saga, [d.id for d in documents], body.tool_names)
    log.info("agent.provision_requested", agent_id=str(agent.id), saga_id=str(saga.id))
    return agent


async def _publish_saga_start(
    db: AsyncSession,
    agent: Agent,
    saga: SagaState,
    document_ids: list[uuid.UUID],
    tool_names: list[str],
) -> None:
    """Send the saga's first command (provision.requested) to RabbitMQ.

    The saga rows are already committed. If the broker is down no message will
    ever arrive, so undo this saga's own effects right away and answer 503,
    instead of leaving the agent PROVISIONING until the timeout reaper notices.
    The message carries the request's correlation id, so one id follows the
    action through gateway -> orchestrator -> worker logs.
    """
    try:
        broker = await get_broker()
        await broker.publish(
            RoutingKey.PROVISION_REQUESTED,
            ProvisionRequested(
                correlation_id=get_correlation_id() or str(agent.id),
                saga_id=saga.id,
                agent_id=agent.id,
                document_ids=document_ids,
                tool_names=tool_names,
            ),
        )
    except Exception as exc:
        log.error("saga.publish_failed", agent_id=str(agent.id), error=str(exc))
        if saga.payload.get("kind") == "update":
            # An edit: drop the documents it added and restore the old status.
            new_ids = [uuid.UUID(i) for i in saga.payload["new_document_ids"]]
            await db.execute(delete(Document).where(Document.id.in_(new_ids)))
            agent.status = AgentStatus(saga.payload["previous_status"])
        else:
            agent.status = AgentStatus.FAILED
        saga.status = SagaStatus.FAILED
        saga.current_step = "publish_failed"
        saga.error = "could not publish the saga's first command"
        await db.commit()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Provisioning is temporarily unavailable. Please try again.",
        ) from exc


@app.get("/v1/agents", response_model=Page)
async def list_agents(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Page:
    base = select(Agent).where(Agent.owner_id == user.id)
    total = await db.scalar(
        select(func.count()).select_from(base.subquery())
    )
    rows = await db.scalars(
        base.order_by(Agent.created_at.desc()).limit(limit).offset(offset)
    )
    items = [AgentOut.model_validate(a) for a in rows.all()]
    return Page(items=items, total=total or 0, limit=limit, offset=offset)


async def _agent_detail(db: AsyncSession, agent: Agent) -> AgentDetail:
    """An agent plus its documents, tools and the outcome of its last saga."""
    doc_rows = await db.execute(
        select(
            Document.id,
            Document.filename,
            func.length(Document.content).label("characters"),
            Document.ingested,
            Document.created_at,
        )
        .where(Document.agent_id == agent.id)
        .order_by(Document.created_at, Document.filename)
    )
    tool_names = await db.scalars(
        select(Tool.name).where(Tool.agent_id == agent.id).order_by(Tool.name)
    )
    last_saga = await db.scalar(
        select(SagaState)
        .where(SagaState.agent_id == agent.id)
        .order_by(SagaState.created_at.desc())
        .limit(1)
    )
    return AgentDetail(
        **AgentOut.model_validate(agent).model_dump(),
        documents=[
            DocumentOut(
                id=r.id,
                filename=r.filename,
                characters=r.characters,
                ingested=r.ingested,
                created_at=r.created_at,
            )
            for r in doc_rows
        ],
        tool_names=list(tool_names),
        last_saga_status=last_saga.status.value if last_saga else None,
        last_saga_error=last_saga.error if last_saga else "",
    )


@app.get("/v1/agents/{agent_id}", response_model=AgentDetail)
async def get_agent(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AgentDetail:
    agent = await db.get(Agent, agent_id)
    if agent is None or agent.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    return await _agent_detail(db, agent)


@app.patch("/v1/agents/{agent_id}", response_model=AgentDetail)
async def update_agent(
    agent_id: uuid.UUID,
    body: AgentUpdate,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AgentDetail:
    """Edit an ACTIVE agent.

    Two kinds of change, handled differently on purpose:

    * name, description, tools, removing documents: instant, in one DB
      transaction. agent-runtime reads these from the database on every query,
      so they take effect on the next question. Answers with 200.
    * adding documents: needs embeddings (slow, can fail), so it runs the same
      Saga as creation, in ``update`` mode. The agent goes back to PROVISIONING
      until the new documents are ingested, then ACTIVE again. If that fails the
      saga removes only the new documents and the agent stays ACTIVE. Answers 202.
    """
    # Lock the agent row so two simultaneous edits cannot both start a saga:
    # the second one waits, then sees PROVISIONING and gets 409.
    agent = await db.get(Agent, agent_id, with_for_update=True)
    if agent is None or agent.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    if agent.status != AgentStatus.ACTIVE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Agent is {agent.status.value}. Only an ACTIVE agent can be edited"
            + (" - delete it and create a new one." if agent.status == AgentStatus.FAILED
               else " - wait until it finishes provisioning."),
        )

    existing_docs = list(
        await db.scalars(select(Document).where(Document.agent_id == agent.id))
    )
    existing_ids = {d.id for d in existing_docs}
    remove_ids = set(body.remove_document_ids)
    unknown = remove_ids - existing_ids
    if unknown:
        raise HTTPException(
            422,
            f"unknown document id(s): {', '.join(sorted(str(i) for i in unknown))}",
        )
    doc_total = len(existing_ids) - len(remove_ids) + len(body.add_documents)
    if doc_total > MAX_DOCUMENTS_PER_AGENT:
        raise HTTPException(
            422,
            f"An agent can have at most {MAX_DOCUMENTS_PER_AGENT} documents.",
        )
    if body.add_documents:
        # New documents are billable embedding calls: same budget as creating.
        enforce(f"create:{user.id}", settings.rate_limit_create_per_hour, 3600)

    # --- instant changes -------------------------------------------------- #
    if body.name is not None:
        agent.name = body.name
    if body.description is not None:
        agent.description = body.description

    current_tools = {
        t.name: t for t in await db.scalars(select(Tool).where(Tool.agent_id == agent.id))
    }
    if body.tool_names is not None:
        for name, tool in current_tools.items():
            if name not in body.tool_names:
                await db.delete(tool)
        for name in body.tool_names:
            if name not in current_tools:
                db.add(Tool(agent_id=agent.id, name=name))
    final_tools = body.tool_names if body.tool_names is not None else list(current_tools)

    if remove_ids:
        # The database's ON DELETE CASCADE removes each document's chunks too.
        await db.execute(delete(Document).where(Document.id.in_(remove_ids)))

    agent.updated_at = datetime.now(UTC)

    # --- slow change: new documents go through the saga ------------------- #
    saga: SagaState | None = None
    new_docs = [
        Document(agent_id=agent.id, filename=d.filename, content=d.content)
        for d in body.add_documents
    ]
    if new_docs:
        db.add_all(new_docs)
        await db.flush()  # get the new document ids
        saga = SagaState(
            agent_id=agent.id,
            status=SagaStatus.RUNNING,
            current_step="start",
            pending_documents=[str(d.id) for d in new_docs],
            payload={
                "kind": "update",
                "previous_status": agent.status.value,
                "new_document_ids": [str(d.id) for d in new_docs],
                "tool_names": final_tools,
            },
        )
        db.add(saga)
        agent.status = AgentStatus.PROVISIONING

    await db.commit()
    await db.refresh(agent)

    if saga is not None:
        await _publish_saga_start(db, agent, saga, [d.id for d in new_docs], final_tools)
        response.status_code = status.HTTP_202_ACCEPTED
        log.info("agent.update_requested", agent_id=str(agent.id), saga_id=str(saga.id))
    else:
        log.info("agent.updated", agent_id=str(agent.id))
    return await _agent_detail(db, agent)


@app.delete("/v1/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    agent = await db.get(Agent, agent_id)
    if agent is None or agent.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    await db.delete(agent)
    await db.commit()


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #
# What the agent "remembers": the last few messages, each trimmed. Enough to
# resolve a follow-up like "and what about the second one?" while keeping the
# prompt (and the bill) small. The full conversation is still stored.
HISTORY_MESSAGES = 6
HISTORY_CHARS = 1500
MAX_MESSAGES_RETURNED = 200


async def _owned_agent(db: AsyncSession, agent_id: uuid.UUID, user: User) -> Agent:
    agent = await db.get(Agent, agent_id)
    if agent is None or agent.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    return agent


async def _latest_messages(
    db: AsyncSession, agent_id: uuid.UUID, limit: int
) -> list[ChatMessage]:
    """The newest ``limit`` messages, returned oldest-first (chat order)."""
    rows = await db.scalars(
        select(ChatMessage)
        .where(ChatMessage.agent_id == agent_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(limit)
    )
    return list(reversed(rows.all()))


async def _ask_runtime(
    agent_id: uuid.UUID, question: str, history: list[dict[str, str]]
) -> QueryOut:
    """Call the agent-runtime service (LangGraph). It is stateless: the history
    travels with the request."""
    url = f"{settings.agent_runtime_url}/v1/agents/{agent_id}/query"
    headers = {"x-correlation-id": get_correlation_id() or str(agent_id)}
    if settings.internal_api_key:
        headers["x-internal-key"] = settings.internal_api_key
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url, json={"question": question, "history": history}, headers=headers
            )
            resp.raise_for_status()
    except httpx.TimeoutException:
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT, "agent-runtime timed out"
        ) from None
    except httpx.HTTPError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"agent-runtime error: {exc}"
        ) from exc
    return QueryOut(**resp.json())


@app.get("/v1/agents/{agent_id}/messages", response_model=list[MessageOut])
async def list_messages(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ChatMessage]:
    """The stored conversation, oldest first (the newest 200 messages)."""
    await _owned_agent(db, agent_id, user)
    return await _latest_messages(db, agent_id, MAX_MESSAGES_RETURNED)


@app.delete("/v1/agents/{agent_id}/messages", status_code=status.HTTP_204_NO_CONTENT)
async def clear_messages(
    agent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Start a fresh conversation: delete this agent's stored messages."""
    await _owned_agent(db, agent_id, user)
    await db.execute(delete(ChatMessage).where(ChatMessage.agent_id == agent_id))
    await db.commit()


@app.post("/v1/agents/{agent_id}/query", response_model=QueryOut)
async def query_agent(
    agent_id: uuid.UUID,
    body: QueryIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> QueryOut:
    """Ask the agent a question and store the exchange.

    The runtime is stateless, so we load the last few messages and send them with
    the question. Both messages are saved only after the runtime answered: a
    failed call leaves no half-finished turn in the history.
    """
    agent = await _owned_agent(db, agent_id, user)
    if agent.status != AgentStatus.ACTIVE:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Agent is {agent.status.value}")

    # Each query costs a model call: limit per user.
    enforce(f"query:{user.id}", settings.rate_limit_query_per_minute, 60)

    recent = await _latest_messages(db, agent_id, HISTORY_MESSAGES)
    history = [{"role": m.role, "content": m.content[:HISTORY_CHARS]} for m in recent]

    asked_at = datetime.now(UTC)  # so the question sorts before its answer
    result = await _ask_runtime(agent_id, body.question, history)

    db.add_all(
        [
            ChatMessage(
                agent_id=agent_id, role="user", content=body.question, created_at=asked_at
            ),
            ChatMessage(
                agent_id=agent_id,
                role="agent",
                content=result.answer,
                sources=[s.model_dump() for s in result.sources],
            ),
        ]
    )
    await db.commit()
    return result
