"""add plan domain type

Revision ID: 20260419_130000
Revises: 20260415_221800
Create Date: 2026-04-19 13:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260419_130000"
down_revision = "20260415_221800"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(
        column["name"] == column_name for column in inspector.get_columns(table_name)
    )


def _index_exists(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    if not _column_exists("olh_plan", "domain_type"):
        op.add_column(
            "olh_plan",
            sa.Column(
                "domain_type",
                sa.String(length=32),
                nullable=False,
                server_default="plan",
            ),
        )
    if not _index_exists("olh_plan", "ix_olh_plan_domain_type"):
        op.create_index(
            "ix_olh_plan_domain_type",
            "olh_plan",
            ["domain_type"],
            unique=False,
        )


def downgrade() -> None:
    if _index_exists("olh_plan", "ix_olh_plan_domain_type"):
        op.drop_index("ix_olh_plan_domain_type", table_name="olh_plan")
    if _column_exists("olh_plan", "domain_type"):
        op.drop_column("olh_plan", "domain_type")
