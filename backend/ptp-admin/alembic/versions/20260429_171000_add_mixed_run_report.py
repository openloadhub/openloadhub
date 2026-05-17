"""add mixed run report table

Revision ID: 20260429_171000
Revises: 20260427_062500
Create Date: 2026-04-29 17:10:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260429_171000"
down_revision = "20260427_062500"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if _table_exists("olh_mixed_run_report"):
        return

    op.create_table(
        "olh_mixed_run_report",
        sa.Column(
            "report_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("mixed_run_id", sa.BigInteger(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("collection_id", sa.BigInteger(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("artifact_path", sa.String(length=500), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("input_sources", sa.JSON(), nullable=True),
        sa.Column("limitations", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("generated_by", sa.BigInteger(), nullable=True),
        sa.Column("generated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
    )
    op.create_index("ix_olh_mixed_run_report_mixed_run_id", "olh_mixed_run_report", ["mixed_run_id"])
    op.create_index("ix_olh_mixed_run_report_round", "olh_mixed_run_report", ["round"])
    op.create_index("ix_olh_mixed_run_report_collection_id", "olh_mixed_run_report", ["collection_id"])
    op.create_index("ix_olh_mixed_run_report_status", "olh_mixed_run_report", ["status"])
    op.create_index("ix_olh_mixed_run_report_generated_by", "olh_mixed_run_report", ["generated_by"])
    op.create_index("ix_olh_mixed_run_report_created_at", "olh_mixed_run_report", ["created_at"])


def downgrade() -> None:
    if not _table_exists("olh_mixed_run_report"):
        return
    op.drop_index("ix_olh_mixed_run_report_created_at", table_name="olh_mixed_run_report")
    op.drop_index("ix_olh_mixed_run_report_generated_by", table_name="olh_mixed_run_report")
    op.drop_index("ix_olh_mixed_run_report_status", table_name="olh_mixed_run_report")
    op.drop_index("ix_olh_mixed_run_report_collection_id", table_name="olh_mixed_run_report")
    op.drop_index("ix_olh_mixed_run_report_round", table_name="olh_mixed_run_report")
    op.drop_index("ix_olh_mixed_run_report_mixed_run_id", table_name="olh_mixed_run_report")
    op.drop_table("olh_mixed_run_report")
