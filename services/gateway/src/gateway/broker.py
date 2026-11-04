"""Lazy singleton Broker for the gateway (publish only)."""

from __future__ import annotations

from common.messaging import Broker
from common.settings import get_settings

_broker: Broker | None = None


async def get_broker() -> Broker:
    global _broker
    if _broker is None:
        _broker = Broker(get_settings().rabbitmq_url)
        await _broker.connect()
    return _broker


async def close_broker() -> None:
    global _broker
    if _broker is not None:
        await _broker.close()
        _broker = None
