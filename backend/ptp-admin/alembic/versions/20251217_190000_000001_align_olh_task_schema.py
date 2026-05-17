"""对齐 olh_task 表结构到 Canonical 契约

Revision ID: 000001
Revises: 000000
Create Date: 2025-12-17 19:00:00

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "000001"
down_revision = "000000"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    try:
        cols = inspector.get_columns(table_name)
    except Exception:
        return set()
    return {c["name"] for c in cols}


def upgrade() -> None:
    existing = _column_names("olh_task")
    if not existing:
        return

    if "env" not in existing:
        op.add_column("olh_task", sa.Column("env", sa.String(length=64), nullable=True, comment="运行环境"))
        op.execute("UPDATE olh_task SET env = 'dev' WHERE env IS NULL")
        op.alter_column("olh_task", "env", existing_type=sa.String(length=64), nullable=False)

    if "task_pattern" not in existing:
        op.add_column(
            "olh_task",
            sa.Column("task_pattern", sa.String(length=64), nullable=True, comment="任务模式（script/visualization）"),
        )
        op.execute("UPDATE olh_task SET task_pattern = 'SCRIPT' WHERE task_pattern IS NULL")
        op.alter_column(
            "olh_task",
            "task_pattern",
            existing_type=sa.String(length=64),
            nullable=False,
        )

    if "protocols" not in existing:
        op.add_column("olh_task", sa.Column("protocols", sa.JSON(), nullable=True, comment="协议类型列表"))
        # MySQL: JSON_ARRAY()；SQLite: 存储为 TEXT 也可接受该表达式（若不支持则保持 NULL）
        try:
            op.execute("UPDATE olh_task SET protocols = JSON_ARRAY() WHERE protocols IS NULL")
        except Exception:
            op.execute("UPDATE olh_task SET protocols = '[]' WHERE protocols IS NULL")
        op.alter_column("olh_task", "protocols", existing_type=sa.JSON(), nullable=False)

    if "collaborator_ids" not in existing:
        op.add_column("olh_task", sa.Column("collaborator_ids", sa.JSON(), nullable=True, comment="协作人 ID 列表"))

    if "last_run_at" not in existing:
        op.add_column("olh_task", sa.Column("last_run_at", sa.DateTime(), nullable=True, comment="最近一次执行时间"))

    if "ix_olh_task_script_id" not in {i.get("name") for i in sa.inspect(op.get_bind()).get_indexes("olh_task")}:
        op.create_index("ix_olh_task_script_id", "olh_task", ["script_id"])


def downgrade() -> None:
    existing = _column_names("olh_task")
    if not existing:
        return

    # 仅回滚本次新增字段；不回滚数据内容
    if "ix_olh_task_script_id" in {i.get("name") for i in sa.inspect(op.get_bind()).get_indexes("olh_task")}:
        op.drop_index("ix_olh_task_script_id", table_name="olh_task")

    for col in ["last_run_at", "collaborator_ids", "protocols", "task_pattern", "env"]:
        if col in existing:
            op.drop_column("olh_task", col)

