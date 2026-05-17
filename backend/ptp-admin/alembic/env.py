import logging
from logging.config import fileConfig
from pathlib import Path
import sys

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

ENV_PATH = Path(__file__).resolve()
PARENTS = ENV_PATH.parents
ROOT_DIR = PARENTS[3] if len(PARENTS) > 3 else PARENTS[1]
BACKEND_DIR = (
    PARENTS[2] if len(PARENTS) > 2 and (PARENTS[2] / "common").exists() else PARENTS[1]
)
for path in (ROOT_DIR, BACKEND_DIR):
    if path.exists():
        str_path = str(path)
        if str_path not in sys.path:
            sys.path.insert(0, str_path)

from common.config.settings import settings
from app.core.database import Base
from app.models.alert_event import RunAlertEvent
from app.models.audit_log import AuditLog
from app.models.mixed_run_report import MixedRunReport
from app.models.plan import Plan
from app.models.plan_run import PlanRun
from app.models.report import Report
from app.models.run import Run
from app.models.run_baseline import RunBaseline
from app.models.script import Script
from app.models.task import Task
from app.models.task_asset import TaskAsset
from app.models.task_version import TaskVersionRecord
from app.models.user import User


target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = settings.DATABASE_URL
    config.set_main_option("sqlalchemy.url", url)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
