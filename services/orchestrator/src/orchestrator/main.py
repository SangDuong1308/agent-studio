"""Orchestrator service: a FastAPI app whose lifespan starts the RabbitMQ
consumer. The HTTP surface is just a health check; the real work is the Saga
consumer running in the background.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from common.logging import configure_logging, get_logger
from common.messaging import Broker
from common.settings import get_settings
from fastapi import FastAPI

from orchestrator.runner import Orchestrator

settings = get_settings()
configure_logging("orchestrator", settings.log_level)
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    broker = Broker(settings.rabbitmq_url)
    await broker.connect()
    orchestrator = Orchestrator(broker)
    await orchestrator.start()
    # Background janitor: fails sagas that stopped making progress.
    reaper = asyncio.create_task(
        orchestrator.run_reaper(
            settings.saga_reaper_interval_seconds, settings.saga_timeout_seconds
        )
    )
    log.info("orchestrator.started")
    app.state.broker = broker
    yield
    reaper.cancel()
    await broker.close()


app = FastAPI(title="Agent Studio Orchestrator", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
