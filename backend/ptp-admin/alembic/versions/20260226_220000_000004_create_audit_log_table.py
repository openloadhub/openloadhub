"""创建审计日志表（olh_audit_log）

Revision ID: 000004
Revises: 000003
Create Date: 2026-02-26 22:00:00

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "000004"
down_revision = "000003"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if _table_exists("olh_audit_log"):
        return

    op.create_table(
        "olh_audit_log",
        sa.Column(
            "id",
            sa.BigInteger(),
            autoincrement=True,
            nullable=False,
            comment="审计记录ID",
        ),
        sa.Column("action", sa.String(length=128), nullable=False, comment="动作标识"),
        sa.Column(
            "outcome",
            sa.String(length=32),
            nullable=False,
            server_default="success",
            comment="结果",
        ),
        sa.Column("actor_id", sa.BigInteger(), nullable=True, comment="操作者ID"),
        sa.Column("actor_role", sa.String(length=64), nullable=True, comment="操作者角色"),
        sa.Column(
            "actor_superuser",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
            comment="是否超级管理员",
        ),
        sa.Column("resource_type", sa.String(length=64), nullable=False, comment="资源类型"),
        sa.Column("resource_id", sa.String(length=128), nullable=True, comment="资源ID"),
        sa.Column("detail", sa.Text(), nullable=True, comment="补充说明"),
        sa.Column("extra", sa.JSON(), nullable=True, comment="扩展上下文"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
            comment="创建时间",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_olh_audit_log_action", "olh_audit_log", ["action"])
    op.create_index("ix_olh_audit_log_outcome", "olh_audit_log", ["outcome"])
    op.create_index("ix_olh_audit_log_actor_id", "olh_audit_log", ["actor_id"])
    op.create_index(
        "ix_olh_audit_log_resource_type", "olh_audit_log", ["resource_type"]
    )
    op.create_index(
        "ix_olh_audit_log_resource_id", "olh_audit_log", ["resource_id"]
    )
    op.create_index("ix_olh_audit_log_created_at", "olh_audit_log", ["created_at"])


def downgrade() -> None:
    if not _table_exists("olh_audit_log"):
        return

    op.drop_index("ix_olh_audit_log_created_at", table_name="olh_audit_log")
    op.drop_index("ix_olh_audit_log_resource_id", table_name="olh_audit_log")
    op.drop_index("ix_olh_audit_log_resource_type", table_name="olh_audit_log")
    op.drop_index("ix_olh_audit_log_actor_id", table_name="olh_audit_log")
    op.drop_index("ix_olh_audit_log_outcome", table_name="olh_audit_log")
    op.drop_index("ix_olh_audit_log_action", table_name="olh_audit_log")
    op.drop_table("olh_audit_log")

