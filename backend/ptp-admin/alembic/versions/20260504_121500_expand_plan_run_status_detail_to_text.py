"""expand plan run status_detail to text

Revision ID: 20260504_121500
Revises: 20260430_060600
Create Date: 2026-05-04 12:15:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260504_121500"
down_revision = "20260430_060600"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    if not _table_exists("olh_plan_run") or _is_sqlite():
        return
    op.alter_column(
        "olh_plan_run",
        "status_detail",
        existing_type=sa.String(length=255),
        type_=sa.Text(),
        existing_nullable=True,
    )


def downgrade() -> None:
    if not _table_exists("olh_plan_run") or _is_sqlite():
        return
    op.alter_column(
        "olh_plan_run",
        "status_detail",
        existing_type=sa.Text(),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
