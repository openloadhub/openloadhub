"""add task asset table

Revision ID: 20260314_220000
Revises: 20260314_160000
Create Date: 2026-03-14 22:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260314_220000"
down_revision = "20260314_160000"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _index_exists(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return index_name in {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    if not _table_exists("olh_task_asset"):
        op.create_table(
            "olh_task_asset",
            sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
            sa.Column("task_id", sa.BigInteger(), nullable=True),
            sa.Column("category", sa.String(length=32), nullable=False),
            sa.Column("file_name", sa.String(length=255), nullable=False),
            sa.Column("file_path", sa.String(length=500), nullable=False),
            sa.Column("file_size", sa.Integer(), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=True),
            sa.Column("created_by", sa.BigInteger(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
            sqlite_autoincrement=True,
        )
    if not _index_exists("olh_task_asset", "ix_olh_task_asset_task_id"):
        op.create_index("ix_olh_task_asset_task_id", "olh_task_asset", ["task_id"])
    if not _index_exists("olh_task_asset", "ix_olh_task_asset_category"):
        op.create_index("ix_olh_task_asset_category", "olh_task_asset", ["category"])
    if not _index_exists("olh_task_asset", "ix_olh_task_asset_created_by"):
        op.create_index("ix_olh_task_asset_created_by", "olh_task_asset", ["created_by"])
    if not _index_exists("olh_task_asset", "ix_olh_task_asset_created_at"):
        op.create_index("ix_olh_task_asset_created_at", "olh_task_asset", ["created_at"])


def downgrade() -> None:
    if _index_exists("olh_task_asset", "ix_olh_task_asset_created_at"):
        op.drop_index("ix_olh_task_asset_created_at", table_name="olh_task_asset")
    if _index_exists("olh_task_asset", "ix_olh_task_asset_created_by"):
        op.drop_index("ix_olh_task_asset_created_by", table_name="olh_task_asset")
    if _index_exists("olh_task_asset", "ix_olh_task_asset_category"):
        op.drop_index("ix_olh_task_asset_category", table_name="olh_task_asset")
    if _index_exists("olh_task_asset", "ix_olh_task_asset_task_id"):
        op.drop_index("ix_olh_task_asset_task_id", table_name="olh_task_asset")
    if _table_exists("olh_task_asset"):
        op.drop_table("olh_task_asset")
