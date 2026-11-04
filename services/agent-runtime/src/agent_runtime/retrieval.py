"""Vector retrieval over pgvector.

Given a question, embed it (as a *query*) and pull the nearest document chunks
for the agent. Uses pgvector's cosine distance operator ``<=>`` via the
pgvector-sqlalchemy ``cosine_distance`` helper.

Each result is a dict: ``content``, ``filename``, ``chunk_index``, ``distance``.
Chunks farther than ``max_distance`` are dropped, so an unrelated question
(or small talk) yields no context instead of the "least bad" chunks.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from common.llm import LLMProvider
from db.models import Document, DocumentChunk
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession


async def retrieve(
    db: AsyncSession,
    provider: LLMProvider,
    agent_id: UUID,
    question: str,
    k: int = 4,
    max_distance: float | None = None,
) -> list[dict[str, Any]]:
    query_vec = await provider.embed_query(question)

    if db.get_bind().dialect.name == "postgresql":
        # The agent_id filter is applied after the HNSW scan. Iterative scan keeps
        # scanning until k rows pass the filter instead of returning fewer.
        # strict_order keeps results sorted by distance. Needs pgvector >= 0.8.
        await db.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))

    distance = DocumentChunk.embedding.cosine_distance(query_vec).label("distance")
    stmt = (
        select(
            DocumentChunk.content,
            DocumentChunk.chunk_index,
            Document.filename,
            distance,
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(DocumentChunk.agent_id == agent_id)
        .order_by(distance)
        .limit(k)
    )
    rows = (await db.execute(stmt)).all()
    results = [
        {
            "content": r.content,
            "chunk_index": r.chunk_index,
            "filename": r.filename,
            "distance": float(r.distance),
        }
        for r in rows
    ]
    if max_distance is not None:
        results = [r for r in results if r["distance"] <= max_distance]
    return results
