"""add started_at index to olh_run

Revision ID: 20260411_074500
Revises: 20260316_193500
Create Date: 2026-04-11 07:45:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260411_074500"
down_revision = "20260316_193500"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _index_exists(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    if _table_exists("olh_run") and not _index_exists("olh_run", "ix_olh_run_started_at"):
        op.create_index("ix_olh_run_started_at", "olh_run", ["started_at"])


def downgrade() -> None:
    if _table_exists("olh_run") and _index_exists("olh_run", "ix_olh_run_started_at"):
        op.drop_index("ix_olh_run_started_at", table_name="olh_run")
