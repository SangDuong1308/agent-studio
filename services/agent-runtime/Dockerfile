# Build context is the repo root.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY packages/common /app/packages/common
COPY packages/db /app/packages/db
RUN pip install ./packages/common ./packages/db

COPY services/agent-runtime /app/services/agent-runtime
# Install with the vertex + langfuse extras so the real AI + tracing paths work
# in the container (LLM_PROVIDER=vertex, LangFuse keys set).
RUN pip install "./services/agent-runtime[vertex,langfuse]"

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8003
CMD ["uvicorn", "agent_runtime.main:app", "--host", "0.0.0.0", "--port", "8003"]
