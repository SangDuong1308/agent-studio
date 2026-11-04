# Build context is the repo root.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY packages/common /app/packages/common
COPY packages/db /app/packages/db
RUN pip install ./packages/common ./packages/db

COPY services/ingestion-worker /app/services/ingestion-worker
RUN pip install "./services/ingestion-worker[vertex]"

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 appuser
USER appuser

CMD ["python", "-m", "ingestion_worker.main"]
