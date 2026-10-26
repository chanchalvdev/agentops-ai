"""evaluation_datasets, evaluation_cases, evaluation_runs, evaluation_results

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-26
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _now() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "evaluation_datasets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("version", sa.String(50), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False),
        _now(),
        sa.UniqueConstraint("name", "sha256", name="uq_eval_datasets_name_sha"),
    )
    op.create_table(
        "evaluation_cases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "dataset_id",
            sa.Uuid(),
            sa.ForeignKey("evaluation_datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("case_id", sa.String(64), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("dataset_id", "case_id", name="uq_eval_cases_dataset_case"),
    )
    op.create_table(
        "evaluation_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("dataset_id", sa.Uuid(), sa.ForeignKey("evaluation_datasets.id"), nullable=False),
        sa.Column("requested_by", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("aggregate", postgresql.JSONB(), nullable=False),
        sa.Column("total_cases", sa.Integer(), nullable=False),
        sa.Column("passed_cases", sa.Integer(), nullable=False),
        sa.Column("pass_rate", sa.Float()),
        sa.Column("error", sa.Text()),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_eval_runs_requested_started", "evaluation_runs", ["requested_by", "started_at"]
    )
    op.create_table(
        "evaluation_results",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("evaluation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("case_id", sa.String(64), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("passed", sa.Boolean()),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("execution_id", sa.Uuid()),
        sa.Column("latency_ms", sa.Float()),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("model_calls", sa.Integer(), nullable=False),
        sa.Column("tool_calls", sa.Integer(), nullable=False),
        sa.Column("evaluators", postgresql.JSONB(), nullable=False),
        sa.Column("judge", postgresql.JSONB()),
        sa.Column("failures", postgresql.JSONB(), nullable=False),
        sa.Column("answer", sa.Text()),
        sa.UniqueConstraint("run_id", "case_id", name="uq_eval_results_run_case"),
    )


def downgrade() -> None:
    op.drop_table("evaluation_results")
    op.drop_table("evaluation_runs")
    op.drop_table("evaluation_cases")
    op.drop_table("evaluation_datasets")
