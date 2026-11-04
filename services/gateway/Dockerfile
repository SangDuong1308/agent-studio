# Build context is the repo root (see docker-compose build.context: ..)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# Install local packages first (better layer caching).
# The db package also carries Alembic + migrations so the init container can run
# `alembic upgrade head` from /app/packages/db.
COPY packages/common /app/packages/common
COPY packages/db /app/packages/db
RUN pip install ./packages/common ./packages/db

COPY services/gateway /app/services/gateway
RUN pip install ./services/gateway

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8001
CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8001"]
