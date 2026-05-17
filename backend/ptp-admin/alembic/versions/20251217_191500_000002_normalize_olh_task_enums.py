"""规范化 olh_task 枚举字段存储为契约值（snake_case）

Revision ID: 000002
Revises: 000001
Create Date: 2025-12-17 19:15:00

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "000002"
down_revision = "000001"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if not _table_exists("olh_task"):
        return

    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return

    # engine_type: JMETER/K6 -> jmeter/k6
    op.execute("ALTER TABLE olh_task MODIFY COLUMN engine_type VARCHAR(32) NOT NULL")
    op.execute(
        """
        UPDATE olh_task
        SET engine_type = LOWER(engine_type)
        WHERE engine_type IN ('JMETER', 'K6')
        """
    )

    # status: 旧值 -> 新契约值；无法映射的统一落到 draft
    op.execute("ALTER TABLE olh_task MODIFY COLUMN status VARCHAR(64) NOT NULL")
    op.execute("UPDATE olh_task SET status = 'running' WHERE status = 'RUNNING'")
    op.execute(
        """
        UPDATE olh_task
        SET status = 'draft'
        WHERE status IN ('PENDING', 'SUCCESS', 'FAILED', 'CANCELLED')
           OR status IS NULL
        """
    )

    # task_pattern: SCRIPT -> script
    op.execute("UPDATE olh_task SET task_pattern = 'script' WHERE task_pattern = 'SCRIPT'")
    op.execute("UPDATE olh_task SET task_pattern = 'script' WHERE task_pattern IS NULL")


def downgrade() -> None:
    # 仅回滚数据值的规范化；不恢复 MySQL ENUM 类型
    if not _table_exists("olh_task"):
        return

    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return

    op.execute("UPDATE olh_task SET task_pattern = 'SCRIPT' WHERE task_pattern = 'script'")
    op.execute("UPDATE olh_task SET engine_type = UPPER(engine_type) WHERE engine_type IN ('jmeter', 'k6')")
    op.execute("UPDATE olh_task SET status = 'PENDING' WHERE status = 'draft'")
    op.execute("UPDATE olh_task SET status = 'RUNNING' WHERE status = 'running'")
