# Build context is the repo root.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY packages/common /app/packages/common
COPY packages/db /app/packages/db
RUN pip install ./packages/common ./packages/db

COPY services/orchestrator /app/services/orchestrator
RUN pip install ./services/orchestrator

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8002
CMD ["uvicorn", "orchestrator.main:app", "--host", "0.0.0.0", "--port", "8002"]
