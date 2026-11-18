"""drift alerts can be acknowledged: who, when, and why

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("monitoring_alerts", sa.Column("acknowledged_by", sa.String(64), nullable=True))
    op.add_column(
        "monitoring_alerts",
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("monitoring_alerts", sa.Column("ack_note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("monitoring_alerts", "ack_note")
    op.drop_column("monitoring_alerts", "acknowledged_at")
    op.drop_column("monitoring_alerts", "acknowledged_by")
