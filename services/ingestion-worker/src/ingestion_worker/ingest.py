"""Core ingestion logic: load a document, chunk it, embed, upsert chunks.

Separated from the messaging glue so it's unit-testable with the FakeProvider
and SQLite. Returns the number of chunks created.
"""

from __future__ import annotations

from uuid import UUID

from common.llm import LLMProvider
from common.logging import get_logger
from db.models import Document, DocumentChunk
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from ingestion_worker.chunking import chunk_text

log = get_logger(__name__)

EMBED_BATCH_SIZE = 50


async def ingest_document(
    db: AsyncSession,
    provider: LLMProvider,
    document_id: UUID,
) -> int:
    """Idempotent: deletes existing chunks for the doc, then re-creates them."""
    doc = await db.get(Document, document_id)
    if doc is None:
        raise ValueError(f"document {document_id} not found")

    # Idempotency: clear any prior chunks (e.g. on redelivery).
    await db.execute(
        delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
    )

    chunks = chunk_text(doc.content)
    if not chunks:
        doc.ingested = True
        return 0

    # Embed in batches: the Vertex API caps texts per request, so one call for a
    # large document would fail.
    vectors: list[list[float]] = []
    for start in range(0, len(chunks), EMBED_BATCH_SIZE):
        vectors.extend(await provider.embed(chunks[start : start + EMBED_BATCH_SIZE]))
    if len(vectors) != len(chunks):
        raise ValueError(
            f"embedding count mismatch: {len(vectors)} vectors for {len(chunks)} chunks"
        )
    for idx, (content, vector) in enumerate(zip(chunks, vectors, strict=True)):
        db.add(
            DocumentChunk(
                document_id=doc.id,
                agent_id=doc.agent_id,
                chunk_index=idx,
                content=content,
                embedding=vector,
            )
        )
    doc.ingested = True
    log.info(
        "ingest.completed",
        document_id=str(document_id),
        chunk_count=len(chunks),
    )
    return len(chunks)
