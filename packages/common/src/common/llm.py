"""Provider-agnostic LLM + embeddings interface.

Services run on Vertex AI (Gemini), but they depend on a small ``LLMProvider``
protocol rather than on a vendor SDK. Swapping Vertex for another provider, or
for the offline stub, is a one-line config change (``LLM_PROVIDER``).

  * ``FakeProvider``  - deterministic, offline. Used by the whole test suite and
                        for local dev without a GCP account.
  * ``VertexProvider``- real Gemini chat + Vertex text embeddings.

Embeddings come in two flavours because retrieval models are trained for
asymmetric search: ``embed`` is for documents that are stored, ``embed_query``
is for the user's question. Using the right one noticeably improves recall.

``get_provider()`` reads ``Settings.llm_provider`` and returns the right one.
"""

from __future__ import annotations

import hashlib
from typing import Protocol

from common.settings import Settings, get_settings

# Size of the vectors stored in ``document_chunks.embedding`` (vector(768)).
# text-embedding-005 returns 768 numbers; the fake matches so the schema is the
# same in tests and production.
EMBEDDING_DIM = 768


class LLMProvider(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per document text (for storing)."""
        ...

    async def embed_query(self, text: str) -> list[float]:
        """Return the embedding of a search query (for retrieval)."""
        ...

    async def chat(self, prompt: str, *, system: str | None = None) -> str:
        """Return a single completion for the prompt."""
        ...


class FakeProvider:
    """Deterministic provider for tests and offline dev.

    Embeddings are derived from a hash of the text so identical text always maps
    to the same vector (stable, repeatable similarity search in tests).
    """

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            # Expand/repeat the 32-byte digest to EMBEDDING_DIM floats in [0,1).
            raw = (digest * ((EMBEDDING_DIM // len(digest)) + 1))[:EMBEDDING_DIM]
            vectors.append([b / 255.0 for b in raw])
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]

    async def chat(self, prompt: str, *, system: str | None = None) -> str:
        return f"[fake-llm] answered: {prompt[:120]}"


class VertexProvider:
    """Real Vertex AI provider. Imports the SDK lazily so the base package has
    no hard dependency on google-cloud when running in fake mode."""

    def __init__(self, settings: Settings) -> None:
        from langchain_google_vertexai import (  # noqa: PLC0415
            ChatVertexAI,
            VertexAIEmbeddings,
        )

        self._embeddings = VertexAIEmbeddings(
            model_name=settings.vertex_embedding_model,
            project=settings.google_cloud_project,
            location=settings.google_cloud_location,
        )
        self._chat = ChatVertexAI(
            model=settings.vertex_chat_model,
            project=settings.google_cloud_project,
            location=settings.google_cloud_location,
            temperature=0.2,
            # Cost + latency guards. Few retries: a 404/403 will not fix itself,
            # and the default (6 retries with backoff) hides real errors for ~30s.
            max_output_tokens=settings.llm_max_output_tokens,
            max_retries=settings.llm_max_retries,
            timeout=settings.llm_timeout_seconds,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self._embeddings.aembed_documents(texts)

    async def embed_query(self, text: str) -> list[float]:
        # Uses task type RETRIEVAL_QUERY (aembed_documents uses RETRIEVAL_DOCUMENT).
        return await self._embeddings.aembed_query(text)

    async def chat(self, prompt: str, *, system: str | None = None) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage  # noqa: PLC0415

        messages = [HumanMessage(content=prompt)]
        if system:
            messages.insert(0, SystemMessage(content=system))
        result = await self._chat.ainvoke(messages)
        # Gemini 3.x returns ``content`` as a list of blocks (text plus thought
        # signatures), not a str. ``.text`` joins just the text blocks.
        return str(result.text)


def get_provider(settings: Settings | None = None) -> LLMProvider:
    settings = settings or get_settings()
    if settings.llm_provider == "vertex":
        return VertexProvider(settings)
    return FakeProvider()
