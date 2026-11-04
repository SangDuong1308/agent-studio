# Architecture

Agent Studio lets a business user create an AI agent, give it documents and
tools, edit it later, and chat with it. Everything below is drawn from the code
as it is today. GitHub renders the diagrams; the system overview is also kept
as a standalone file, [`architecture.mmd`](architecture.mmd).

## 1. The system

```mermaid
flowchart LR
  subgraph client["1 · Client"]
    ui["Next.js (App Router)<br/>React Query · Zustand"]
  end

  subgraph edge["2 · API edge"]
    gw["Gateway · FastAPI<br/>JWT · rate limits · validation · CORS<br/>agents CRUD + edit · chat history<br/>starts sagas"]
  end

  subgraph bus["3 · Messaging"]
    mq{{"RabbitMQ<br/>topic exchange<br/>+ dead-letter queues"}}
  end

  subgraph svc["4 · Backend services"]
    orch["Orchestrator<br/>Saga: provision / update<br/>+ timeout reaper"]
    ing["Ingestion worker<br/>chunk → embed → store"]
    rt["Agent runtime · LangGraph<br/>supervisor → rewriter → retriever → writer"]
  end

  subgraph data["5 · Data"]
    pg[("PostgreSQL + pgvector<br/>users · agents · documents · chunks (HNSW)<br/>tools · chat_messages · saga_state")]
  end

  subgraph ext["6 · External (GCP)"]
    vx["Vertex AI<br/>Gemini · text-embedding-005"]
    lf["LangFuse<br/>traces"]
  end

  ui -- "REST + JWT" --> gw
  gw -- "CRUD · save / load chat" --> pg
  gw -- "provision.requested" --> mq
  gw -- "question + recent history<br/>+ X-Internal-Key" --> rt

  mq <-- "in: provision.requested, ingestion.succeeded / failed<br/>out: ingestion.requested" --> orch
  mq <-- "in: ingestion.requested<br/>out: ingestion.succeeded / failed" --> ing

  orch -- "saga state · activate · compensate" --> pg
  ing -- "store vectors" --> pg
  ing -- "embed documents" --> vx
  rt -- "retrieve top-k" --> pg
  rt -- "embed question · generate answer" --> vx
  rt -. "traces" .-> lf
```

**In one breath:** the gateway is the only public service. Anything slow
(embedding documents) is handed to RabbitMQ so the user gets an immediate answer
and can watch the progress. The orchestrator coordinates that slow work as a
**saga** and keeps its state in Postgres, so it can be rolled back if it fails.

Only IDs travel on the queue. The document text stays in Postgres and the worker
reads it from there, so messages stay small.

## 2. Creating an agent (the saga)

```mermaid
sequenceDiagram
  autonumber
  actor U as User
  participant UI as Next.js
  participant GW as Gateway
  participant DB as PostgreSQL
  participant MQ as RabbitMQ
  participant OR as Orchestrator
  participant IW as Ingestion worker
  participant VX as Vertex AI

  U->>UI: fill the form, click Create
  UI->>GW: POST /v1/agents
  GW->>DB: agent (PROVISIONING) + documents + tools + saga_state (RUNNING)
  GW->>MQ: provision.requested
  GW-->>UI: 202 Accepted
  loop every 2 seconds
    UI->>GW: GET /v1/agents/{id}
    GW-->>UI: status = PROVISIONING
  end
  MQ->>OR: provision.requested
  OR->>MQ: ingestion.requested (one per document)
  MQ->>IW: ingestion.requested
  IW->>DB: read the document text
  IW->>VX: embed the chunks
  IW->>DB: store chunks and vectors
  IW->>MQ: ingestion.succeeded
  MQ->>OR: ingestion.succeeded
  OR->>DB: last document done → agent ACTIVE, saga COMPLETED
  UI->>GW: GET /v1/agents/{id}
  GW-->>UI: status = ACTIVE → chat is enabled
```

**Why a saga?** Creating an agent touches several services (database, queue,
embedding API). There is no single transaction across them, so if step 3 of 4
fails you must undo steps 1 and 2 yourself. That undo is the *compensation*.

## 3. Editing an agent

```mermaid
flowchart TD
  A["PATCH /v1/agents/{id}"] --> B{"agent is ACTIVE?"}
  B -- "no" --> X["409 Conflict<br/>(still provisioning, or failed)"]
  B -- "yes" --> C["apply instantly, one transaction:<br/>name · description · tools · remove documents"]
  C --> D{"adding documents?"}
  D -- "no" --> E["200 OK<br/>agent stays ACTIVE"]
  D -- "yes" --> F["agent → PROVISIONING<br/>start a saga of kind 'update'<br/>202 Accepted"]
  F --> G["ingest ONLY the new documents<br/>(same orchestrator, worker and queue)"]
  G --> H{"done before the timeout?"}
  H -- "yes" --> I["agent → ACTIVE<br/>old knowledge untouched"]
  H -- "no, or ingestion failed" --> J["compensate: delete ONLY the new documents<br/>agent → ACTIVE again<br/>UI shows 'your last change was not applied'"]
```

**The design choice worth explaining:** name, description and tools are read from
the database on every question, so changing them needs no background work and
takes effect immediately. Only *new documents* need embeddings, which is slow and
can fail, so only that part goes through the saga. When it fails, the saga undoes
just what it added: one bad upload must not destroy an agent that was working.

While an edit saga runs, further edits are refused with `409` (the agent row is
locked while the request is checked, so two simultaneous edits cannot both start).

## 4. The life of a saga

```mermaid
stateDiagram-v2
  [*] --> RUNNING: gateway creates the saga

  RUNNING --> COMPLETED: every document ingested
  RUNNING --> FAILED: ingestion.failed event
  RUNNING --> FAILED: no progress for SAGA_TIMEOUT_SECONDS<br/>(the reaper)

  COMPLETED --> [*]
  FAILED --> [*]

  note right of FAILED
    provision: everything rolled back, agent FAILED
    update: only the new documents removed, agent ACTIVE
  end note
  note left of COMPLETED
    Only a RUNNING saga reacts to events.
    Late or duplicate messages for a COMPLETED or
    FAILED saga are ignored (late chunks are deleted).
  end note
```

Three rules keep it trustworthy when RabbitMQ delivers messages late, twice, or
never:

1. **Only a RUNNING saga reacts to events.** A stray "failed" message cannot
   wreck an agent that is already ACTIVE, and a worker that finishes *after* a
   rollback has its orphan chunks deleted.
2. **Each handler locks the saga row** (`SELECT … FOR UPDATE`), so two events for
   the same saga are processed one at a time.
3. **A saga that stops making progress is failed by a timeout.** The orchestrator
   runs a small loop every `SAGA_REAPER_INTERVAL_SECONDS` that rolls back any
   RUNNING saga idle for `SAGA_TIMEOUT_SECONDS`. This covers a lost message, a
   crashed worker, or a poison message sitting in a dead-letter queue.

## 5. Asking a question (and how the chat remembers)

```mermaid
sequenceDiagram
  autonumber
  participant UI as Next.js
  participant GW as Gateway
  participant RT as Agent runtime
  participant DB as PostgreSQL
  participant VX as Vertex AI

  UI->>GW: GET /v1/agents/{id}/messages (when the chat opens)
  GW->>DB: newest 200 messages of this agent
  GW-->>UI: the saved conversation

  UI->>GW: POST /v1/agents/{id}/query (JWT)
  GW->>GW: check login, ownership, rate limit, agent is ACTIVE
  GW->>DB: last 6 messages (the agent's memory)
  GW->>RT: question + history + X-Internal-Key + correlation id
  RT->>DB: load the agent's name, description, tools
  RT->>RT: supervisor picks a route
  alt arithmetic and the calculator tool is attached
    RT->>RT: run the calculator (no model call)
  else a knowledge question
    opt there is history
      RT->>VX: rewrite the follow-up as a standalone search query
    end
    RT->>VX: embed the search query
    RT->>DB: nearest chunks by cosine distance (HNSW)
    RT->>RT: drop chunks that are too far away
    RT->>VX: Gemini, with a system prompt + history + retrieved passages
  end
  RT-->>GW: answer + retrieved passages
  GW->>DB: save the question and the answer
  GW-->>UI: answer + retrieved passages
```

The runtime is not public: it rejects any call without the shared
`X-Internal-Key` that only the gateway adds, and its port is not published.

**How the memory works**

- **The gateway owns the conversation, the runtime stays stateless.** The gateway
  already knows who the user is and which agent they own, so it stores the
  messages (`chat_messages`, one conversation per agent) and sends the last 6 with
  each question. The runtime needs no database table for chat and can be scaled
  or restarted freely.
- **Everything is stored, only a little is remembered.** The screen reloads the
  newest 200 messages; the agent sees the last 6, each cut to 1,500 characters,
  which is enough for "and what about the second one?" without a big prompt.
  "Clear chat" deletes the stored messages, so the agent forgets them too.
- **A follow-up is rewritten before searching.** "and for digital products?" has
  no meaning to a vector search, so with history the model first turns it into
  "refund window for digital products". The first question of a conversation skips
  this step (nothing to resolve, one model call saved), and if the rewrite fails
  the original question is searched instead.
- **Saved only after the answer succeeded.** If the runtime is down or times out,
  nothing is written, so a failed question never sits in the history. The UI keeps
  the typed text so the user can simply retry.

## 6. Where to look in the code

| Topic | File |
|---|---|
| Public API, auth, rate limits, edit, chat history | `services/gateway/src/gateway/main.py` |
| Saga rules, compensation, timeout query | `services/orchestrator/src/orchestrator/saga.py` |
| Event handlers and the reaper loop | `services/orchestrator/src/orchestrator/runner.py` |
| Chunk → embed → store | `services/ingestion-worker/src/ingestion_worker/` |
| LangGraph route / rewrite / retrieve / write | `services/agent-runtime/src/agent_runtime/graph.py` |
| RabbitMQ topology, DLQs | `packages/common/src/common/messaging.py` |
| Message contracts | `packages/common/src/common/messages.py` |
| Tables and indexes | `packages/db/src/db/models.py`, `packages/db/migrations/` |
| What the edit form sends | `frontend/src/lib/agentDiff.ts` |
| Chat screen (loads the saved conversation) | `frontend/src/app/agents/[id]/page.tsx` |
