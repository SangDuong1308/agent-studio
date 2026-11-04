# Design decisions

Notes on why the system is built this way, and what each choice costs. Each
section states the decision, the reasoning, and the trade-off.

## Why orchestration-based Saga (not choreography)?
A central orchestrator makes the provisioning flow and its compensations
explicit and easy to trace. For a platform team operating many agents, "where
is this agent stuck and what will roll back?" has a single answer (the
`saga_state` row). Choreography (services reacting to events with no coordinator)
is more decoupled but harder to reason about and debug. Tradeoff: the
orchestrator is a potential bottleneck/single point of coordination — mitigated
by keeping it stateless apart from the DB and able to resume from `saga_state`.

## Why RabbitMQ between services (not direct HTTP)?
Ingestion is slow and bursty. Async messaging decouples the gateway's fast
"create agent" response (202 Accepted) from the slow ingestion work, lets us
scale ingestion workers independently (competing consumers), and gives
at-least-once delivery with dead-letter queues for poison messages. Direct HTTP
would couple availability and make retries/backpressure the caller's problem.

## Why pgvector (not a dedicated vector DB)?
Keeping vectors in Postgres means one datastore, one backup story, and
transactional consistency between an agent's rows and its chunks. It also
strengthens the "schema design + indexes" story (HNSW index, cosine ops). We
started with IVFFlat and switched in migration `0002`: IVFFlat needs training
data (it was built on an empty table, so its clusters were meaningless), and
because every query filters by `agent_id`, an approximate index can return fewer
than `k` rows. HNSW with pgvector's iterative scan keeps scanning until enough
rows pass the filter. A dedicated vector DB (Pinecone, Weaviate) wins at very
large scale; for this platform's size, pgvector is the simpler, correct choice.

## Why a provider-agnostic LLM interface?
The service runs on Vertex AI, but nothing outside `common/llm.py` imports a
vendor SDK. Hiding the provider behind a small protocol means swapping Vertex for
another vendor, or for a deterministic fake, is one config line
(`LLM_PROVIDER`). The fake provider also makes the entire test suite offline and
deterministic.

## Why idempotent, persisted saga steps?
RabbitMQ is at-least-once, so a message can be redelivered. Each step checks
`completed_steps` before applying, and ingestion deletes-then-reinserts chunks,
so redelivery is safe. Persisting saga state means a crashed orchestrator can be
restarted without losing or corrupting in-flight provisioning.

## Why does editing an agent only sometimes use the saga?
Name, description and tools are read from the database on every question, so
changing them needs no background work: one transaction, takes effect on the next
question, answers `200`. Only *adding documents* needs embeddings, which is slow
and can fail, so only that goes through the saga (`202`, agent is `PROVISIONING`
until indexed). Removing a document is instant (the database cascades to its
chunks). Rule of thumb: **use the saga for the part that is slow and can fail**,
not for everything.

## Why does a failed edit not mark the agent FAILED?
The two sagas have different things to undo. A failed *creation* has nothing
worth keeping, so everything is rolled back and the agent is `FAILED`. A failed
*edit* is on an agent that was working: the saga deletes only the documents it
added and puts the agent back to `ACTIVE`, so one bad upload cannot destroy a
working agent. The user is told through `last_saga_status` / `last_saga_error`
(the UI shows "your last change was not applied").

## Why can only an ACTIVE agent be edited?
While a saga runs, a second edit would start a second saga over the same agent.
The gateway locks the agent row (`SELECT … FOR UPDATE`) while it checks the
status, so two simultaneous edits cannot both pass: the second sees
`PROVISIONING` and gets `409`. A `FAILED` agent is rejected too (delete and
recreate); retrying a failed creation would be a separate feature.

## Why a timeout, and why a reaper loop?
A message queue cannot promise that every saga finishes: a message can be lost, a
worker can crash mid-job, a poison message can sit in a dead-letter queue. Without
a timeout the agent would stay `PROVISIONING` forever. The orchestrator therefore
runs a loop that fails any `RUNNING` saga with no progress for
`SAGA_TIMEOUT_SECONDS` (default 300) and runs the normal compensation. "No
progress" is measured from `updated_at`, which moves each time a document is
ingested, so a slow-but-working saga is not killed. It is safe with several
orchestrator replicas because each saga is locked and re-checked before it is
rolled back. Alternative considered: a transactional outbox, which fixes the
"committed to the DB but never published" gap at the source; the reaper is the
simpler safety net that also covers every other way a saga can stall.

## Why does only a RUNNING saga react to events?
RabbitMQ is at-least-once: messages can arrive late or twice. If any event could
act on any saga, a stray "ingestion failed" could roll back an agent that is
already `ACTIVE`, and a worker finishing *after* a rollback would revive it or
leave orphan chunks. So handlers ignore events for sagas that are not `RUNNING`
(and delete the orphan chunks of a saga that already `FAILED`).

## Why does one correlation id follow a request through every service?
The gateway puts the request's correlation id on the RabbitMQ message, and each
service logs it. One `grep <id>` across gateway, orchestrator and worker logs
reconstructs what happened to one user action.

## Why does the gateway store the chat, and the runtime stay stateless?
The gateway already knows who the user is and which agent they own, so it is the
natural owner of the conversation: it loads the last few messages, sends them with
the question, and saves the exchange. The runtime then only has to turn "question
+ history" into an answer, which keeps it free of chat tables and easy to scale or
restart. Alternative considered: letting the runtime read and write the history
itself. That would put a second service in charge of user data and force it to
repeat the ownership check.

## Why one conversation per agent, and why only the last 6 messages?
Only an agent's owner can chat with it, so a single thread per agent needs just an
`agent_id` on each message and a "Clear chat" button for a fresh start. Several
named conversations (a sidebar of threads) would be a bigger UI and a
`conversations` table for little gain here; it is the obvious next step if needed.
Everything is stored, but the model sees only the last 6 messages, each cut to
1,500 characters: enough to resolve "and what about the second one?", while the
prompt (and the bill) stays small. Summarising old turns would be the upgrade for
very long chats.

## Why rewrite a follow-up before searching?
A vector search for "and for digital products?" has nothing to match. With
history, one cheap model call turns it into "refund window for digital products"
before retrieval. The first message of a conversation skips the call entirely, and
if the rewrite fails or comes back empty or absurdly long, the original question is
searched, so the feature can only help, never block an answer. The writer still
answers the user's own words, with the history in its prompt.

## Why save the question and the answer together, only after success?
If the question were saved first and the runtime then failed, the history would
hold a question with no answer, and the next call would carry it along as if it had
been handled. Saving both after the runtime answered keeps the history consistent;
the UI keeps the typed text so the user can retry.

## Why separate API schemas from DB models?
The wire contract (Pydantic schemas in `gateway/schemas.py`) can evolve
independently of storage (SQLAlchemy models), and we never leak columns like
`hashed_password`. This is standard defensive API design.

## Where correctness was actually verified

**Unit tests** (`make test`): 80 backend + 15 frontend. They run on SQLite with a
fake LLM, so they cover logic (saga rules, edit endpoint, diff logic) but not
pgvector/HNSW, row locking or RabbitMQ. I also checked the tests can fail: removing
the "only a RUNNING saga reacts" guard, or making edits compensate like creations,
makes the relevant tests fail.

**Real stack** (docker compose: real Postgres, RabbitMQ, Vertex AI), driven through
the browser with Playwright on 2026-11-04:
- register → create agent → saga → `ACTIVE` → grounded answer with passages → tool
- edit name, description and tools: instant (`200`), no saga, new tool works on the
  very next question
- add a document: `202`, agent `PROVISIONING` then `ACTIVE`; only the new document
  was embedded; the agent answered from the new *and* the old document; one
  correlation id appeared in the gateway, orchestrator and worker logs
- remove a document: instant, its chunks cascade-deleted, the agent no longer
  knows that topic
- worker stopped + `SAGA_TIMEOUT_SECONDS=20`: a stuck **edit** was rolled back to
  `ACTIVE` (banner "your last change was not applied", old data intact) and a stuck
  **creation** became `FAILED` with its tools removed
- worker restarted afterwards, delivering results *late*: the stray "ingestion
  failed" for the rolled-back edit was ignored (agent stayed `ACTIVE`), and the
  chunks the slow worker wrote for the failed creation were deleted (0 orphans);
  both dead-letter queues stayed empty
- edit while `PROVISIONING` → `409`; edit page of a non-`ACTIVE` agent redirects
  to the agent page; delete cascades to every table

**Chat memory**, same stack, also driven through the browser (2026-11-04):
- the saved conversation, with its retrieved passages, reloads identically after a
  page refresh and after logging out and in again; rows in `chat_messages` are in
  order, each question stamped before its answer
- a calculator answer is stored like any other message
- memory makes a visible difference: after "How much does express shipping cost?",
  the follow-up "and how long does it take?" answers "2 business days" (express
  only); asked cold, the same follow-up lists standard *and* express
- the rewriter, run against real Gemini, turned "and for digital products?" into
  "refund window for digital products" and left a first question untouched
- agent-runtime stopped: the UI shows an error toast, keeps the typed text and shows
  no orphan question; the table stays unchanged; after the runtime restarts, the
  retry from the same input works
- another user gets `404` on read and clear; "Clear chat" asks for confirmation;
  deleting the agent deletes its messages
- **Not a proof of the rewriter on its own:** in that test the whole policy fit in a
  single chunk, so retrieval returned it for any wording. The rewrite is covered by
  unit tests and the direct Gemini call above, but not by a multi-chunk end-to-end
  case yet.

**Not verified yet:** deployment to GKE, behaviour under load, documents large
enough to produce many chunks, and two orchestrator replicas running at once (the
locking is designed for it but was only tested with one).
