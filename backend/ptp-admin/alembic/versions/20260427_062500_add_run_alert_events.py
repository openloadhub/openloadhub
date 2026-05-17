"""add run alert events table

Revision ID: 20260427_062500
Revises: 20260426_175500
Create Date: 2026-04-27 06:25:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260427_062500"
down_revision = "20260426_175500"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if _table_exists("run_alert_events"):
        return

    op.create_table(
        "run_alert_events",
        sa.Column("event_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("task_id", sa.BigInteger(), nullable=True),
        sa.Column("mixed_run_id", sa.BigInteger(), nullable=True),
        sa.Column("plan_run_id", sa.BigInteger(), nullable=True),
        sa.Column("subscription", sa.String(length=128), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("alertname", sa.String(length=255), nullable=True),
        sa.Column("severity", sa.String(length=64), nullable=True),
        sa.Column("priority", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=True),
        sa.Column("starts_at", sa.DateTime(), nullable=True),
        sa.Column("ends_at", sa.DateTime(), nullable=True),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("annotations", sa.JSON(), nullable=False),
        sa.Column("dashboard_url", sa.String(length=1024), nullable=True),
        sa.Column("fingerprint", sa.String(length=128), nullable=False),
        sa.Column("source_event_id", sa.String(length=255), nullable=True),
        sa.Column("dedupe_key", sa.String(length=512), nullable=False),
        sa.Column("aggregation_key", sa.String(length=512), nullable=False),
        sa.Column("action_status", sa.String(length=64), nullable=False),
        sa.Column("raw_event", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()
        ),
    )
    for column in (
        "run_id",
        "task_id",
        "mixed_run_id",
        "plan_run_id",
        "subscription",
        "source",
        "alertname",
        "severity",
        "priority",
        "status",
        "starts_at",
        "fingerprint",
        "source_event_id",
        "dedupe_key",
        "aggregation_key",
        "action_status",
        "created_at",
    ):
        op.create_index(f"ix_run_alert_events_{column}", "run_alert_events", [column])


def downgrade() -> None:
    if not _table_exists("run_alert_events"):
        return
    for column in (
        "created_at",
        "action_status",
        "aggregation_key",
        "dedupe_key",
        "source_event_id",
        "fingerprint",
        "starts_at",
        "status",
        "priority",
        "severity",
        "alertname",
        "source",
        "subscription",
        "plan_run_id",
        "mixed_run_id",
        "task_id",
        "run_id",
    ):
        op.drop_index(f"ix_run_alert_events_{column}", table_name="run_alert_events")
    op.drop_table("run_alert_events")
