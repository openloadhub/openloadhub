"""add webhook config retry

Revision ID: 20260426_060000
Revises: 20260425_092500
Create Date: 2026-04-26 06:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260426_060000"
down_revision = "20260425_092500"
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
        op.create_table(
            "olh_webhook_config",
            sa.Column(
                "config_id", sa.BigInteger(), primary_key=True, autoincrement=True
            ),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("channel", sa.String(length=32), nullable=False),
            sa.Column("event_types", sa.JSON(), nullable=False),
            sa.Column("webhook_url", sa.String(length=2048), nullable=False),
            sa.Column(
                "enabled", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("template", sa.Text(), nullable=True),
            sa.Column("title", sa.String(length=128), nullable=True),
            sa.Column(
                "timeout_seconds", sa.Float(), nullable=False, server_default="5"
            ),
            sa.Column(
                "max_retry_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "retry_interval_seconds",
                sa.Float(),
                nullable=False,
                server_default="0",
            ),
            sa.Column("created_by", sa.BigInteger(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=True,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_olh_webhook_config_channel", "olh_webhook_config", ["channel"]
        )
        op.create_index(
            "ix_olh_webhook_config_enabled", "olh_webhook_config", ["enabled"]
        )
        op.create_index(
            "ix_olh_webhook_config_created_by", "olh_webhook_config", ["created_by"]
        )
        op.create_index(
            "ix_olh_webhook_config_created_at", "olh_webhook_config", ["created_at"]
        )

    if _table_exists("olh_webhook_send_record"):
        if not _column_exists("olh_webhook_send_record", "config_id"):
            op.add_column(
                "olh_webhook_send_record",
                sa.Column("config_id", sa.BigInteger(), nullable=True),
            )
            op.create_index(
                "ix_olh_webhook_send_record_config_id",
                "olh_webhook_send_record",
                ["config_id"],
            )
        if not _column_exists("olh_webhook_send_record", "trigger_source"):
            op.add_column(
                "olh_webhook_send_record",
                sa.Column(
                    "trigger_source",
                    sa.String(length=32),
                    nullable=False,
                    server_default="manual",
                ),
            )
            op.create_index(
                "ix_olh_webhook_send_record_trigger_source",
                "olh_webhook_send_record",
                ["trigger_source"],
            )


def downgrade() -> None:
    if _table_exists("olh_webhook_send_record"):
        if _column_exists("olh_webhook_send_record", "trigger_source"):
            op.drop_index(
                "ix_olh_webhook_send_record_trigger_source",
                table_name="olh_webhook_send_record",
            )
            op.drop_column("olh_webhook_send_record", "trigger_source")
        if _column_exists("olh_webhook_send_record", "config_id"):
            op.drop_index(
                "ix_olh_webhook_send_record_config_id",
                table_name="olh_webhook_send_record",
            )
            op.drop_column("olh_webhook_send_record", "config_id")

    if _table_exists("olh_webhook_config"):
        op.drop_index(
            "ix_olh_webhook_config_created_at", table_name="olh_webhook_config"
        )
        op.drop_index(
            "ix_olh_webhook_config_created_by", table_name="olh_webhook_config"
        )
        op.drop_index("ix_olh_webhook_config_enabled", table_name="olh_webhook_config")
        op.drop_index("ix_olh_webhook_config_channel", table_name="olh_webhook_config")
        op.drop_table("olh_webhook_config")
