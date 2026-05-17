"""add webhook send record table

Revision ID: 20260425_092500
Revises: 20260419_130000
Create Date: 2026-04-25 09:25:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260425_092500"
down_revision = "20260419_130000"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if _table_exists("olh_webhook_send_record"):
        return

    op.create_table(
        "olh_webhook_send_record",
        sa.Column("record_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="pending"
        ),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("rendered_text", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("variables", sa.JSON(), nullable=True),
        sa.Column("webhook_url_masked", sa.String(length=512), nullable=True),
        sa.Column("webhook_host", sa.String(length=255), nullable=True),
        sa.Column("http_status_code", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()
        ),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_olh_webhook_send_record_channel", "olh_webhook_send_record", ["channel"]
    )
    op.create_index(
        "ix_olh_webhook_send_record_event_type",
        "olh_webhook_send_record",
        ["event_type"],
    )
    op.create_index(
        "ix_olh_webhook_send_record_status", "olh_webhook_send_record", ["status"]
    )
    op.create_index(
        "ix_olh_webhook_send_record_webhook_host",
        "olh_webhook_send_record",
        ["webhook_host"],
    )
    op.create_index(
        "ix_olh_webhook_send_record_created_by",
        "olh_webhook_send_record",
        ["created_by"],
    )
    op.create_index(
        "ix_olh_webhook_send_record_created_at",
        "olh_webhook_send_record",
        ["created_at"],
    )


def downgrade() -> None:
    if not _table_exists("olh_webhook_send_record"):
        return
    op.drop_index(
        "ix_olh_webhook_send_record_created_at", table_name="olh_webhook_send_record"
    )
    op.drop_index(
        "ix_olh_webhook_send_record_created_by", table_name="olh_webhook_send_record"
    )
    op.drop_index(
        "ix_olh_webhook_send_record_webhook_host", table_name="olh_webhook_send_record"
    )
    op.drop_index(
        "ix_olh_webhook_send_record_status", table_name="olh_webhook_send_record"
    )
    op.drop_index(
        "ix_olh_webhook_send_record_event_type", table_name="olh_webhook_send_record"
    )
    op.drop_index(
        "ix_olh_webhook_send_record_channel", table_name="olh_webhook_send_record"
    )
    op.drop_table("olh_webhook_send_record")
