"""add plan metadata fields

Revision ID: 20260316_193500
Revises: 20260314_220000
Create Date: 2026-03-16 19:35:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260316_193500"
down_revision = "20260314_220000"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    if not _column_exists("olh_plan", "business_lines"):
        op.add_column("olh_plan", sa.Column("business_lines", sa.JSON(), nullable=True))
    if not _column_exists("olh_plan", "collaborator_ids"):
        op.add_column("olh_plan", sa.Column("collaborator_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    if _column_exists("olh_plan", "collaborator_ids"):
        op.drop_column("olh_plan", "collaborator_ids")
    if _column_exists("olh_plan", "business_lines"):
        op.drop_column("olh_plan", "business_lines")
