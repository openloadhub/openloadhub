"""add task version history table

Revision ID: 20260314_160000
Revises: 20260313_add_report_run_id
Create Date: 2026-03-14 16:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260314_160000"
down_revision = "20260313_add_report_run_id"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _index_exists(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return index_name in {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    if not _table_exists("olh_task_version_history"):
        op.create_table(
            "olh_task_version_history",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("task_id", sa.BigInteger(), nullable=False),
            sa.Column("version", sa.String(length=50), nullable=False),
            sa.Column("created_by", sa.BigInteger(), nullable=True),
            sa.Column("task_snapshot", sa.Text(), nullable=False),
            sa.Column("script_content", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        )
    if not _index_exists("olh_task_version_history", "ix_olh_task_version_history_task_id"):
        op.create_index(
            "ix_olh_task_version_history_task_id",
            "olh_task_version_history",
            ["task_id"],
            unique=False,
        )
    if not _index_exists("olh_task_version_history", "ix_olh_task_version_history_created_at"):
        op.create_index(
            "ix_olh_task_version_history_created_at",
            "olh_task_version_history",
            ["created_at"],
            unique=False,
        )


def downgrade() -> None:
    if _index_exists("olh_task_version_history", "ix_olh_task_version_history_created_at"):
        op.drop_index(
            "ix_olh_task_version_history_created_at",
            table_name="olh_task_version_history",
        )
    if _index_exists("olh_task_version_history", "ix_olh_task_version_history_task_id"):
        op.drop_index(
            "ix_olh_task_version_history_task_id",
            table_name="olh_task_version_history",
        )
    if _table_exists("olh_task_version_history"):
        op.drop_table("olh_task_version_history")
