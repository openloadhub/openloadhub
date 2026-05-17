"""创建 Runs/Plans/PlanRuns 表（olh_run/olh_plan/olh_plan_run）

Revision ID: 000003
Revises: 000002
Create Date: 2025-12-17 19:30:00

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "000003"
down_revision = "000002"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if not _table_exists("olh_run"):
        op.create_table(
            "olh_run",
            sa.Column("run_id", sa.BigInteger(), autoincrement=True, nullable=False, comment="执行记录ID"),
            sa.Column("task_id", sa.BigInteger(), nullable=False, comment="任务ID"),
            sa.Column("task_name", sa.String(length=255), nullable=True, comment="任务名（可选冗余）"),
            sa.Column("engine_type", sa.String(length=32), nullable=False, comment="引擎类型"),
            sa.Column("protocol", sa.String(length=32), nullable=True, comment="协议类型（可选）"),
            sa.Column("env", sa.String(length=64), nullable=False, comment="运行环境"),
            sa.Column("run_status", sa.String(length=32), nullable=False, comment="主状态"),
            sa.Column("run_status_detail", sa.String(length=64), nullable=True, comment="细分阶段（可选）"),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("ended_at", sa.DateTime(), nullable=True),
            sa.Column("duration_seconds", sa.Integer(), nullable=True),
            sa.Column("params", sa.JSON(), nullable=True, comment="本次运行参数快照"),
            sa.Column("total_requests", sa.Integer(), nullable=True),
            sa.Column("success_rate", sa.Float(), nullable=True),
            sa.Column("error_rate", sa.Float(), nullable=True),
            sa.Column("avg_rt_ms", sa.Float(), nullable=True),
            sa.Column("p95_rt_ms", sa.Float(), nullable=True),
            sa.Column("p99_rt_ms", sa.Float(), nullable=True),
            sa.Column("rps", sa.Float(), nullable=True),
            sa.Column("stop_reason", sa.Text(), nullable=True),
            sa.Column("idempotency_key", sa.String(length=128), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=True),
            sa.PrimaryKeyConstraint("run_id"),
            sa.UniqueConstraint("idempotency_key"),
        )
        op.create_index("ix_olh_run_task_id", "olh_run", ["task_id"])
        op.create_index("ix_olh_run_run_status", "olh_run", ["run_status"])
        op.create_index("ix_olh_run_created_at", "olh_run", ["created_at"])
        op.create_index("ix_olh_run_idempotency_key", "olh_run", ["idempotency_key"])

    if not _table_exists("olh_plan"):
        op.create_table(
            "olh_plan",
            sa.Column("plan_id", sa.BigInteger(), autoincrement=True, nullable=False, comment="计划ID"),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False, comment="计划状态"),
            sa.Column("exec_type", sa.String(length=32), nullable=False, comment="执行类型"),
            sa.Column("cron", sa.String(length=128), nullable=True),
            sa.Column("scheduled_at", sa.DateTime(), nullable=True),
            sa.Column("timezone", sa.String(length=64), nullable=True),
            sa.Column("enable_round", sa.Boolean(), nullable=True),
            sa.Column("total_round", sa.Integer(), nullable=True),
            sa.Column("stages", sa.JSON(), nullable=False),
            sa.Column("created_by", sa.BigInteger(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=True),
            sa.PrimaryKeyConstraint("plan_id"),
        )
        op.create_index("ix_olh_plan_status", "olh_plan", ["status"])
        op.create_index("ix_olh_plan_created_at", "olh_plan", ["created_at"])

    if not _table_exists("olh_plan_run"):
        op.create_table(
            "olh_plan_run",
            sa.Column("plan_run_id", sa.BigInteger(), autoincrement=True, nullable=False, comment="计划执行记录ID"),
            sa.Column("plan_id", sa.BigInteger(), nullable=False),
            sa.Column("plan_name", sa.String(length=255), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False, comment="执行状态"),
            sa.Column("status_detail", sa.String(length=64), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("ended_at", sa.DateTime(), nullable=True),
            sa.Column("duration_seconds", sa.Integer(), nullable=True),
            sa.Column("round", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("created_by", sa.BigInteger(), nullable=True),
            sa.PrimaryKeyConstraint("plan_run_id"),
        )
        op.create_index("ix_olh_plan_run_plan_id", "olh_plan_run", ["plan_id"])
        op.create_index("ix_olh_plan_run_status", "olh_plan_run", ["status"])
        op.create_index("ix_olh_plan_run_created_at", "olh_plan_run", ["created_at"])


def downgrade() -> None:
    if _table_exists("olh_plan_run"):
        op.drop_index("ix_olh_plan_run_created_at", table_name="olh_plan_run")
        op.drop_index("ix_olh_plan_run_status", table_name="olh_plan_run")
        op.drop_index("ix_olh_plan_run_plan_id", table_name="olh_plan_run")
        op.drop_table("olh_plan_run")

    if _table_exists("olh_plan"):
        op.drop_index("ix_olh_plan_created_at", table_name="olh_plan")
        op.drop_index("ix_olh_plan_status", table_name="olh_plan")
        op.drop_table("olh_plan")

    if _table_exists("olh_run"):
        op.drop_index("ix_olh_run_idempotency_key", table_name="olh_run")
        op.drop_index("ix_olh_run_created_at", table_name="olh_run")
        op.drop_index("ix_olh_run_run_status", table_name="olh_run")
        op.drop_index("ix_olh_run_task_id", table_name="olh_run")
        op.drop_table("olh_run")

