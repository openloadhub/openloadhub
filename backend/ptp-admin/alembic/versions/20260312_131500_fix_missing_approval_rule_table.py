"""repair missing approval rule table after bad migration

Revision ID: 20260312_fix_approval_rule
Revises: 0e8db5c5137c
Create Date: 2026-03-12 13:15:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260312_fix_approval_rule"
down_revision = "0e8db5c5137c"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if _table_exists("olh_approval_rule"):
        return

    op.create_table(
        "olh_approval_rule",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False, comment="规则名称"),
        sa.Column("description", sa.Text(), nullable=True, comment="规则描述"),
        sa.Column(
            "condition_type",
            sa.Enum(
                "ENV",
                "THREAD_COUNT",
                "DURATION",
                "ENGINE_TYPE",
                "ALWAYS",
                name="conditiontype",
            ),
            nullable=False,
            comment="条件类型",
        ),
        sa.Column(
            "condition_op",
            sa.Enum("EQ", "NEQ", "GT", "GTE", "LT", "LTE", "IN", name="conditionop"),
            nullable=False,
            server_default="EQ",
            comment="条件操作符",
        ),
        sa.Column(
            "condition_value",
            sa.String(length=500),
            nullable=False,
            comment="条件值（字符串，多值逗号分隔）",
        ),
        sa.Column(
            "approver_roles", sa.JSON(), nullable=True, comment="审批角色列表 JSON"
        ),
        sa.Column(
            "priority",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="优先级（数字越大优先级越高）",
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
            comment="是否启用",
        ),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=True,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    if _table_exists("olh_approval_rule"):
        op.drop_table("olh_approval_rule")
