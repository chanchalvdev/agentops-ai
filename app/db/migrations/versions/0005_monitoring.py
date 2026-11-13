"""per-case cost/usage on evaluation results; retrieval evaluation runs; drift checks; alerts

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-11
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def _now() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    # Day 3 deferral: per-case cost was computed but not persisted.
    op.add_column("evaluation_results", sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True))
    op.add_column("evaluation_results", sa.Column("usage", postgresql.JSONB(), nullable=True))

    op.create_table(
        "retrieval_eval_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("requested_by", sa.String(64), nullable=False),
        sa.Column("dataset_name", sa.String(100), nullable=False),
        sa.Column("dataset_version", sa.String(50), nullable=False),
        sa.Column("dataset_sha256", sa.String(64), nullable=False),
        sa.Column("profile", sa.String(64), nullable=True),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        sa.Column("cases", postgresql.JSONB(), nullable=False),
        _now(),
    )
    op.create_index("ix_retrieval_eval_runs_created", "retrieval_eval_runs", ["created_at"])

    op.create_table(
        "drift_checks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("requested_by", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),  # ok | drift | suspected
        sa.Column("baseline", postgresql.JSONB(), nullable=False),
        sa.Column("signals", postgresql.JSONB(), nullable=False),
        _now(),
    )
    op.create_index("ix_drift_checks_created", "drift_checks", ["created_at"])

    op.create_table(
        "monitoring_alerts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "check_id",
            sa.Uuid(),
            sa.ForeignKey("drift_checks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("metric", sa.String(100), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),  # critical | warning | info
        sa.Column("evidence", sa.String(16), nullable=False),  # measured | suspected
        sa.Column("baseline_value", sa.Float(), nullable=True),
        sa.Column("current_value", sa.Float(), nullable=True),
        sa.Column("threshold", sa.String(100), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        _now(),
    )
    op.create_index("ix_monitoring_alerts_created", "monitoring_alerts", ["created_at"])


def downgrade() -> None:
    op.drop_table("monitoring_alerts")
    op.drop_table("drift_checks")
    op.drop_table("retrieval_eval_runs")
    op.drop_column("evaluation_results", "usage")
    op.drop_column("evaluation_results", "cost_usd")
