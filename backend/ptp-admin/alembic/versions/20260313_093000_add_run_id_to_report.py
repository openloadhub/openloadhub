"""add run_id to report for precise runs download mapping

Revision ID: 20260313_add_report_run_id
Revises: 20260312_fix_approval_rule
Create Date: 2026-03-13 09:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260313_add_report_run_id"
down_revision = "20260312_fix_approval_rule"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def _index_exists(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return index_name in {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    if not _column_exists("olh_report", "run_id"):
        op.add_column(
            "olh_report",
            sa.Column("run_id", sa.BigInteger(), nullable=True, comment="关联的执行记录ID"),
        )
    if not _index_exists("olh_report", "ix_olh_report_run_id"):
        op.create_index("ix_olh_report_run_id", "olh_report", ["run_id"], unique=False)


def downgrade() -> None:
    if _index_exists("olh_report", "ix_olh_report_run_id"):
        op.drop_index("ix_olh_report_run_id", table_name="olh_report")
    if _column_exists("olh_report", "run_id"):
        op.drop_column("olh_report", "run_id")
