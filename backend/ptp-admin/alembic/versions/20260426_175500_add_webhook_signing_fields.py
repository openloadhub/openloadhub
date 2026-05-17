"""add webhook signing fields

Revision ID: 20260426_175500
Revises: 20260426_060000
Create Date: 2026-04-26 17:55:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260426_175500"
down_revision = "20260426_060000"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {
        column["name"] for column in inspector.get_columns(table_name)
    }


def upgrade() -> None:
    if not _table_exists("olh_webhook_config"):
        return
    if not _column_exists("olh_webhook_config", "signature_type"):
        op.add_column(
            "olh_webhook_config",
            sa.Column(
                "signature_type",
                sa.String(length=32),
                nullable=False,
                server_default="none",
            ),
        )
    if not _column_exists("olh_webhook_config", "signing_secret"):
        op.add_column(
            "olh_webhook_config",
            sa.Column("signing_secret", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    if not _table_exists("olh_webhook_config"):
        return
    if _column_exists("olh_webhook_config", "signing_secret"):
        op.drop_column("olh_webhook_config", "signing_secret")
    if _column_exists("olh_webhook_config", "signature_type"):
        op.drop_column("olh_webhook_config", "signature_type")
