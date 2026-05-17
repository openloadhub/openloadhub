"""add olh_run_baseline table

Revision ID: 20260415_221800
Revises: 20260411_074500
Create Date: 2026-04-15 22:18:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260415_221800"
down_revision = "20260411_074500"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if _table_exists("olh_run_baseline"):
        return

    op.create_table(
        "olh_run_baseline",
        sa.Column("baseline_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("scope_type", sa.String(length=32), nullable=False),
        sa.Column("scope_key", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("env", sa.String(length=64), nullable=False),
        sa.Column("protocol", sa.String(length=32), nullable=True),
        sa.Column("baseline_run_id", sa.BigInteger(), nullable=False),
        sa.Column("baseline_source", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("effective_from", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.UniqueConstraint("scope_type", "scope_key", name="uq_olh_run_baseline_scope"),
    )
    op.create_index("ix_olh_run_baseline_scope_type", "olh_run_baseline", ["scope_type"])
    op.create_index("ix_olh_run_baseline_scope_key", "olh_run_baseline", ["scope_key"])
    op.create_index("ix_olh_run_baseline_task_id", "olh_run_baseline", ["task_id"])
    op.create_index("ix_olh_run_baseline_env", "olh_run_baseline", ["env"])
    op.create_index("ix_olh_run_baseline_protocol", "olh_run_baseline", ["protocol"])
    op.create_index("ix_olh_run_baseline_baseline_run_id", "olh_run_baseline", ["baseline_run_id"])
    op.create_index("ix_olh_run_baseline_effective_from", "olh_run_baseline", ["effective_from"])


def downgrade() -> None:
    if not _table_exists("olh_run_baseline"):
        return
    op.drop_index("ix_olh_run_baseline_effective_from", table_name="olh_run_baseline")
    op.drop_index("ix_olh_run_baseline_baseline_run_id", table_name="olh_run_baseline")
    op.drop_index("ix_olh_run_baseline_protocol", table_name="olh_run_baseline")
    op.drop_index("ix_olh_run_baseline_env", table_name="olh_run_baseline")
    op.drop_index("ix_olh_run_baseline_task_id", table_name="olh_run_baseline")
    op.drop_index("ix_olh_run_baseline_scope_key", table_name="olh_run_baseline")
    op.drop_index("ix_olh_run_baseline_scope_type", table_name="olh_run_baseline")
    op.drop_table("olh_run_baseline")
