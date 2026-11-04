# Agent Studio

A self-hosted platform where teams create AI agents, attach tools, upload internal
documents, and chat with agents that answer from those documents.

Creating an agent is handled as a distributed workflow rather than a single
insert: the agent and its documents are persisted, each document is chunked and
embedded into PostgreSQL, tools are registered, and the agent is activated — with
compensating actions when a step fails, so no half-built agent is left behind.
Questions run through a LangGraph pipeline that retrieves the relevant passages
and answers from them.

The model provider sits behind a small interface, so the same code runs on Vertex
AI (Gemini), another vendor, or a deterministic offline stub without changes.

---

## 1. What problem does it solve?

Business teams want their own AI agents without waiting on engineering.
Provisioning an agent is not a single insert: the agent has to be persisted, each
document ingested, chunked and embedded, the vector index populated, tools
registered, and only then activated. If the third of five steps fails, the earlier
work must be undone so no half-built agent is left behind.

That sequence is the reason for **RabbitMQ** between services and for an
orchestration-based **Saga** with compensating actions, and it is why
**LangGraph** coordinates the retrieval and answering steps at query time.

---

## 2. Architecture

Four independently deployable services + Postgres + RabbitMQ + a Next.js UI.
Full diagrams (system, create flow, edit flow, saga lifecycle, chat flow) with a
plain-English walkthrough are in **[`docs/architecture.md`](docs/architecture.md)**.

![System overview](docs/architecture.png)

```
 Next.js UI ──REST+JWT──▶ gateway ──┬── CRUD ─────────▶ PostgreSQL (+ pgvector)
                                    ├── provision cmd ─▶ RabbitMQ ─▶ orchestrator (Saga)
                                    │                                     │
                                    │                      ingest job ◀───┘ (RabbitMQ)
                                    │                            │
                                    │                      ingestion-worker ─▶ embed (Vertex AI)
                                    │                            └────────────▶ upsert vectors (Postgres)
                                    └── chat + memory ─▶ agent-runtime (LangGraph) ─▶ Gemini + pgvector
                                                                                   └─▶ LangFuse (traces)
```

| Service | Role | Port |
|---|---|---|
| `gateway` | Public REST API: auth (JWT/OAuth2), agents CRUD + edit, saga kickoff, chat history, query proxy | 8001 |
| `orchestrator` | The Saga orchestrator: drives provisioning and edits, compensates on failure, times out stuck sagas | 8002 |
| `ingestion-worker` | Consumes ingest jobs: chunk → embed → upsert to pgvector | — |
| `agent-runtime` | LangGraph graph (supervisor → rewriter → retriever → writer): retrieval + Gemini + tools + LangFuse | 8003 |

### The Saga (orchestration-based)

One saga mechanism, two kinds, because they have different things to undo:

```
provision  (create an agent)                 update  (add documents to an ACTIVE agent)
  1 create agent + documents (gateway)         1 create the new documents (gateway)
  2 ingest documents      comp: delete chunks  2 ingest ONLY the new documents
  3 register tools        comp: delete tools                          comp: delete only the new documents
  4 activate agent                             3 activate agent
  on failure: everything rolled back,          on failure: agent goes back to ACTIVE,
  agent marked FAILED                          keeps all its old knowledge
```

Editing an agent: name, description, tools and removing documents are applied
instantly (the runtime reads them from the database on every question). Only
*adding documents* needs embeddings, so only that goes through the saga.

Three rules keep the saga correct when RabbitMQ delivers messages late, twice or
never (all covered by unit tests in `services/orchestrator/tests/`):

1. **Only a RUNNING saga reacts to events.** A stray or duplicate "failed" message
   cannot wreck an agent that is already ACTIVE; a worker that finishes after a
   rollback has its orphan chunks deleted.
2. **Each handler locks the saga row** (`SELECT … FOR UPDATE`), so concurrent
   events for one saga are processed one at a time.
3. **Timeout:** a saga with no progress for `SAGA_TIMEOUT_SECONDS` (default 300)
   is failed and compensated by a reaper loop in the orchestrator. This covers a
   lost message, a crashed worker, or a poison message in a dead-letter queue.

Saga state is persisted in `saga_state`, so the flow is durable and idempotent.

### Chat memory

Each agent has one saved conversation (`chat_messages`). The **gateway** stores
it and sends the last 6 messages to the **runtime** with every question; the
runtime keeps no chat state. With history, the runtime first rewrites a follow-up
("and for digital products?") into a standalone search query, so retrieval still
finds the right passage. A message pair is saved only after the runtime answered,
so a failed call leaves nothing half-finished. "Clear chat" deletes the messages.

---

## 3. Tech stack

- **Backend:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0 (async), Alembic, pytest
- **AI:** LangGraph + LangChain, `langchain-google-vertexai` (Gemini + `text-embedding-005`)
- **Messaging:** RabbitMQ via `aio-pika` (topic exchange, dead-letter queues)
- **Data:** PostgreSQL + pgvector (HNSW index for cosine similarity)
- **Frontend:** Next.js 14 (App Router), TypeScript, React Query, Zustand
- **Observability:** LangFuse (LangChain CallbackHandler) + structured JSON logs with correlation IDs
- **Ops:** Docker, docker-compose, GitHub Actions, Kubernetes (GKE)

---

## 4. Repository layout

```
agent-studio/
├── packages/
│   ├── common/        # settings, logging, messaging (RabbitMQ), message contracts, LLM interface
│   └── db/            # SQLAlchemy models, async session, Alembic migrations
├── services/
│   ├── gateway/       # FastAPI: auth, agents CRUD + edit, saga kickoff
│   ├── orchestrator/  # Saga orchestrator + RabbitMQ consumer
│   ├── ingestion-worker/ # chunk + embed + upsert to pgvector
│   └── agent-runtime/ # LangGraph execution (route → retrieve → write)
├── frontend/          # Next.js App Router app
├── docs/              # architecture.md (Mermaid diagrams + walkthrough), architecture.mmd
├── deploy/
│   ├── docker-compose.yml
│   └── k8s/           # GKE manifests
├── .github/workflows/ # ci.yml (tests) + deploy.yml (build/push/deploy)
├── Makefile
└── .env.example
```

---

## 5. Run it locally — step by step

### Prerequisites
- Docker + Docker Compose
- Python 3.11+ and Node 20+ (only if running services on the host instead of Docker)

### Option A — everything in Docker (simplest)

```bash
cp .env.example .env          # defaults use the offline "fake" LLM provider
docker compose -f deploy/docker-compose.yml --env-file .env up --build
```

Then open:
- Frontend: http://localhost:3000
- Gateway API docs (OpenAPI/Swagger): http://localhost:8001/docs
- RabbitMQ management UI: http://localhost:15672 (guest / guest)

> Database migrations run automatically: a one-shot `migrate` service applies
> them before the app services start, so a fresh machine is ready after one `up`.

### Option B — infra in Docker, services + DB migration on the host

```bash
# 1. Install everything into a venv + frontend deps
make install

# 2. Start Postgres + RabbitMQ only
make infra-up

# 3. Create the schema (pgvector extension, tables, vector index)
make migrate

# 4. Run the backend services (each in its own terminal)
. .venv/bin/activate
uvicorn gateway.main:app       --port 8001   # terminal 1
uvicorn orchestrator.main:app  --port 8002   # terminal 2
python -m ingestion_worker.main              # terminal 3
uvicorn agent_runtime.main:app --port 8003   # terminal 4

# 5. Run the frontend
make fe-dev    # http://localhost:3000
```

Set the host env so services reach the containers:
`export DATABASE_URL=postgresql+asyncpg://agent:agent@localhost:5432/agentstudio`
`export RABBITMQ_URL=amqp://guest:guest@localhost:5672/`

---

## 6. Try the full flow from the API (no UI needed)

```bash
BASE=http://localhost:8001

# register + login
curl -X POST $BASE/v1/auth/register -H 'Content-Type: application/json' \
  -d '{"email":"you@test.com","password":"password123"}'
TOKEN=$(curl -s -X POST $BASE/v1/auth/login \
  -d 'username=you@test.com&password=password123' | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# create an agent with a document + the calculator tool (kicks off the saga)
AID=$(curl -s -X POST $BASE/v1/agents -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Support Bot","documents":[{"filename":"faq.txt","content":"Refunds are allowed within 30 days."}],"tool_names":["calculator"]}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

# poll until ACTIVE
curl -s $BASE/v1/agents/$AID -H "Authorization: Bearer $TOKEN"

# ask a question grounded in the document
curl -s -X POST $BASE/v1/agents/$AID/query -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"question":"What is the refund policy?"}'

# use the calculator tool
curl -s -X POST $BASE/v1/agents/$AID/query -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"question":"What is 21 * 2?"}'

# the conversation is saved: reload it, or start over
curl -s $BASE/v1/agents/$AID/messages -H "Authorization: Bearer $TOKEN"
curl -s -X DELETE $BASE/v1/agents/$AID/messages -H "Authorization: Bearer $TOKEN"   # 204

# edit the agent: rename it and add the word_count tool (instant, 200)
curl -s -X PATCH $BASE/v1/agents/$AID -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Support Bot v2","tool_names":["calculator","word_count"]}'

# edit the agent: add a document (starts an update saga, 202; the agent is
# PROVISIONING until it is indexed, then ACTIVE again - poll GET /v1/agents/$AID)
curl -s -X PATCH $BASE/v1/agents/$AID -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"add_documents":[{"filename":"shipping.txt","content":"Orders ship in 2 days."}]}'
```

---

## 7. Test & evaluate

```bash
make test          # backend (4 suites) + frontend
make test-be       # backend only
make test-fe       # frontend only
```

What the suites cover:
- **gateway**: auth flow, authorization (401), pagination, validation (422), saga command published; **editing** (instant vs saga changes, 409 while provisioning, 404 for other users, broker-down rollback); **chat history** (stored in order, last 6 sent as memory, nothing stored when the runtime fails, private per user, clear, deleted with the agent)
- **orchestrator**: Saga happy path, waiting for all documents, **compensation** on failure (provision *and* update), late/duplicate events ignored, **timeout reaper**
- **ingestion-worker**: chunking, embedding dimension, upsert, idempotency
- **agent-runtime**: LangGraph routing (RAG path vs tool path), tool invocation, calculator formatting, **follow-up rewriting** (and its fallbacks) and history in the prompt
- **frontend**: status helpers, the edit-form diff logic, API error formatting

Unit tests run on SQLite, so they do not exercise pgvector/HNSW, `FOR UPDATE`
locking or RabbitMQ. Those were checked by running the real stack end to end;
see "Where correctness was actually verified" in [`DECISIONS.md`](DECISIONS.md).

**Evaluating the AI output:** with `LLM_PROVIDER=fake` answers are deterministic
(prefixed `[fake-llm]`) so tests are stable. To see real Gemini answers and
LangFuse traces, set `LLM_PROVIDER=vertex` and the GCP/LangFuse env vars, then
inspect each run (nodes, tool calls, latency, cost) in the LangFuse dashboard.

---

## Security and operations notes

- **Ports:** `docker compose up` publishes only the frontend (3000) and gateway
  (8001), plus Postgres/RabbitMQ for local tooling, all on `127.0.0.1`.
  `agent-runtime` and `orchestrator` are not published.
- **Service-to-service auth:** `agent-runtime` rejects requests without the
  `INTERNAL_API_KEY` header the gateway adds. Set the same value everywhere.
- **Migrations** run automatically through the one-shot `migrate` service before
  the app services start. To run them by hand: `make migrate`.
- **Limits:** per-IP login/register and per-user query/create rate limits, a
  document size cap (200,000 characters, 10 per agent) and an agent quota per
  user. All are env-configurable (see `.env.example`).
- **Production mode:** `APP_ENV=production` makes services refuse to start with
  the default JWT secret, no `INTERNAL_API_KEY`, or `CORS_ORIGINS=*`.
- **Model lifecycle:** Google retires model versions on a fixed schedule
  (check the [model versions page](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/model-versions)).
  `text-embedding-005` retires on 2027-04-01; changing the embedding model means
  re-embedding every document.

---

## 8. Switch on the real AI (Vertex AI + LangFuse)

1. Create a GCP project, enable the **Vertex AI API**.
2. Create a service account with the **Vertex AI User** role, download its JSON key.
3. In `.env`:
   ```
   LLM_PROVIDER=vertex
   GOOGLE_CLOUD_PROJECT=your-project-id
   GOOGLE_CLOUD_LOCATION=global   # Gemini 3.x isn't served from us-central1
   GOOGLE_APPLICATION_CREDENTIALS=/secrets/gcp-key.json
   VERTEX_CHAT_MODEL=gemini-3.5-flash-lite
   VERTEX_EMBEDDING_MODEL=text-embedding-005   # 768 dims, matches vector(768)
   INTERNAL_API_KEY=<openssl rand -hex 32>     # gateway -> agent-runtime shared secret
   ```
   Mount the key into the containers at `/secrets/gcp-key.json`.
4. (Optional) LangFuse: create a project at cloud.langfuse.com, set
   `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`. Leave blank to disable tracing.
5. Install the Vertex extra: `pip install -e "./services/agent-runtime[vertex,langfuse]"`
   (or rebuild the image with those extras). This pulls LangFuse v3, whose
   LangChain handler lives at `langfuse.langchain` (v2's `langfuse.callback` is
   gone). Auth is read from the `LANGFUSE_*` env vars; `observability.py` handles
   this and degrades to no-op tracing if the keys are blank.

---

## 9. Deploy to Google Cloud (GKE)

See `deploy/k8s/README.md` for the full walkthrough. Short version:

```bash
# one-time: create an Artifact Registry repo + a GKE cluster
gcloud artifacts repositories create agent-studio --repository-format=docker --location=us-central1
gcloud container clusters create-auto agent-studio --region=us-central1

# build + push images, then apply manifests (CI does this automatically via deploy.yml)
# set these GitHub repo secrets: GCP_PROJECT, GCP_REGION, GKE_CLUSTER, GCP_SA_KEY
```

The CI pipeline (`.github/workflows/`):
- **ci.yml** runs all tests + the frontend build on every push/PR.
- **deploy.yml** builds/pushes the five images to Artifact Registry and deploys
  to GKE, then waits for the gateway rollout. It runs **manually**
  (`workflow_dispatch`); the push-to-`main` trigger is commented out until a
  cluster is set up.

For production Postgres, use **Cloud SQL** + the Cloud SQL Auth Proxy instead of
the in-cluster StatefulSet, and point `DATABASE_URL` at it.

---

## 10. Investigating issues through logs

Every service emits **structured JSON** logs. A `correlation_id` is attached at
the gateway and propagated across HTTP headers and RabbitMQ messages, so one
user action is traceable across all services:

```bash
docker compose -f deploy/docker-compose.yml logs orchestrator | grep <correlation_id>
# in GKE:
kubectl -n agent-studio logs deploy/orchestrator | grep <correlation_id>
```

Poison messages (malformed or repeatedly failing) are routed to per-consumer
**dead-letter queues** (`orchestrator.dlq`, `ingestion-worker.dlq`) rather than
being retried forever — inspect them in the RabbitMQ management UI.
