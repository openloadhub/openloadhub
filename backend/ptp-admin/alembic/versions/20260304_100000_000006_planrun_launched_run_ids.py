"""PlanRun 新增 launched_run_ids + status_detail 扩容

Revision ID: 000006
Revises: 000005
Create Date: 2026-03-04 10:00:00

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "000006"
down_revision = "000005"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = [c["name"] for c in inspector.get_columns(table)]
    return column in columns


def upgrade() -> None:
    if not _column_exists("olh_plan_run", "launched_run_ids"):
        op.add_column(
            "olh_plan_run",
            sa.Column("launched_run_ids", sa.JSON(), nullable=True,
                       comment="已启动的 Run ID 列表"),
        )
    # status_detail 从 64 扩容到 128
    # MySQL ALTER, SQLite 不支持但无影响
    try:
        op.alter_column(
            "olh_plan_run", "status_detail",
            existing_type=sa.String(64),
            type_=sa.String(128),
        )
    except Exception:
        pass  # SQLite 不支持 ALTER COLUMN


def downgrade() -> None:
    if _column_exists("olh_plan_run", "launched_run_ids"):
        op.drop_column("olh_plan_run", "launched_run_ids")
