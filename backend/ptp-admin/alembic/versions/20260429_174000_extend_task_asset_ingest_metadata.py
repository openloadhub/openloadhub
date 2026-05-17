"""extend task asset ingest metadata

Revision ID: 20260429_174000
Revises: 20260429_171000
Create Date: 2026-04-29 17:40:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260429_174000"
down_revision = "20260429_171000"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column_name in {
        column["name"] for column in inspector.get_columns(table_name)
    }


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if not _column_exists(table_name, column.name):
        op.add_column(table_name, column)


def upgrade() -> None:
    if not _table_exists("olh_task_asset"):
        return

    if not _is_sqlite():
        op.alter_column(
            "olh_task_asset",
            "file_size",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=False,
        )

    _add_column_if_missing(
        "olh_task_asset",
        sa.Column(
            "storage_type",
            sa.String(length=32),
            nullable=False,
            server_default="local",
        ),
    )
    _add_column_if_missing(
        "olh_task_asset",
        sa.Column("compression_type", sa.String(length=32), nullable=True),
    )
    _add_column_if_missing(
        "olh_task_asset",
        sa.Column("compressed_file_size", sa.BigInteger(), nullable=True),
    )
    _add_column_if_missing(
        "olh_task_asset",
        sa.Column("line_count", sa.BigInteger(), nullable=True),
    )
    _add_column_if_missing(
        "olh_task_asset",
        sa.Column(
            "ingest_status",
            sa.String(length=32),
            nullable=False,
            server_default="completed",
        ),
    )
    _add_column_if_missing(
        "olh_task_asset",
        sa.Column("ingest_error", sa.Text(), nullable=True),
    )
    _add_column_if_missing(
        "olh_task_asset",
        sa.Column("metadata_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    if not _table_exists("olh_task_asset"):
        return

    for column_name in (
        "metadata_json",
        "ingest_error",
        "ingest_status",
        "line_count",
        "compressed_file_size",
        "compression_type",
        "storage_type",
    ):
        if _column_exists("olh_task_asset", column_name):
            op.drop_column("olh_task_asset", column_name)

    if not _is_sqlite():
        op.alter_column(
            "olh_task_asset",
            "file_size",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=False,
        )
