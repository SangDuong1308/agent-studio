"""switch the embedding index from ivfflat to HNSW

Why:
  * ivfflat needs training data. It was created on an empty table, so its
    cluster centres are meaningless and recall is poor.
  * Queries filter by ``agent_id``. With an approximate index the filter is
    applied AFTER the index scan, so a query can return fewer than ``k`` rows.
    pgvector >= 0.8 supports iterative scans on HNSW, which keep scanning until
    enough rows pass the filter (retrieval sets ``hnsw.iterative_scan``).

Only the index changes. The column stays ``vector(768)`` and no data is touched.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-31 00:00:00
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding")
    op.execute(
        "CREATE INDEX ix_document_chunks_embedding "
        "ON document_chunks USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding")
    op.execute(
        "CREATE INDEX ix_document_chunks_embedding "
        "ON document_chunks USING ivfflat (embedding vector_cosine_ops) "
        "WITH (lists = 100)"
    )
