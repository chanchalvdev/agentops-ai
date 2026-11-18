"""durable indexing: the documents table is the work queue (lease + attempts), and
`searchable` keeps a document's previous index serving while it is reindexed

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("searchable", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "documents", sa.Column("attempts", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("documents", sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE documents SET searchable = (status = 'ready')")
    op.create_index(
        "ix_documents_queue",
        "documents",
        ["status", "lease_until"],
        postgresql_where=sa.text("status IN ('pending', 'processing')"),
    )


def downgrade() -> None:
    op.drop_index("ix_documents_queue", table_name="documents")
    op.drop_column("documents", "lease_until")
    op.drop_column("documents", "attempts")
    op.drop_column("documents", "searchable")
