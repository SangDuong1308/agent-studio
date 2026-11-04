from __future__ import annotations

import os

from common.logging import get_logger
from common.settings import get_settings

log = get_logger(__name__)


def build_callbacks(
    session_id: str, tags: list[str] | None = None
) -> tuple[list, dict]:
    """Return ``(callbacks, metadata)`` for a graph invocation.

    ``callbacks`` goes into ``config["callbacks"]`` and ``metadata`` into
    ``config["metadata"]``. Both are empty when LangFuse is disabled, so callers
    can always spread them unconditionally.
    """
    settings = get_settings()
    if not settings.langfuse_enabled:
        return [], {}

    # v3 reads credentials from the environment; mirror Settings into env.
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key)
    os.environ.setdefault("LANGFUSE_HOST", settings.langfuse_host)

    try:
        from langfuse.langchain import CallbackHandler  # noqa: PLC0415

        handler = CallbackHandler()
        metadata = {
            "langfuse_session_id": session_id,
            "langfuse_tags": tags or [],
        }
        return [handler], metadata
    except Exception as exc:  # pragma: no cover - optional dependency/path
        log.warning("langfuse.init_failed", error=str(exc))
        return [], {}
