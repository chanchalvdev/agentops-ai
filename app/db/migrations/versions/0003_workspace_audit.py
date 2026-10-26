"""workspace_audit

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(16), nullable=False),
        sa.Column("execution_id", sa.Uuid()),
        sa.Column("key_id", sa.String(32)),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("exit_code", sa.Integer()),
        sa.Column("bytes", sa.Integer()),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_workspace_audit_tenant_id", "workspace_audit", ["tenant_id", "id"])


def downgrade() -> None:
    op.drop_table("workspace_audit")
