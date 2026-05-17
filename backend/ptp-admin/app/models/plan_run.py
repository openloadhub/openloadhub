from __future__ import annotations

from pathlib import Path
import sys

COMMON_PARENT = Path(__file__).resolve().parents[3]
if COMMON_PARENT.exists():
    sys.path.append(str(COMMON_PARENT))

from sqlalchemy import BigInteger, Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.sql import func
from sqlalchemy.types import Enum as SQLEnum

from app.core.database import Base
from common.models.enums import PlanRunStatus


class PlanRun(Base):
    __tablename__ = "olh_plan_run"
    __table_args__ = {"sqlite_autoincrement": True}

    plan_run_id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
        comment="计划执行记录ID",
    )

    plan_id = Column(BigInteger, nullable=False, index=True)
    plan_name = Column(String(255), nullable=True)

    status = Column(
        SQLEnum(
            PlanRunStatus,
            native_enum=False,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        default=PlanRunStatus.PREPARING,
        index=True,
    )
    status_detail = Column(Text, nullable=True)
    launched_run_ids = Column(JSON, nullable=True, default=list, comment="已启动的 Run ID 列表")
    stages_snapshot = Column(JSON, nullable=True, default=list, comment="执行时的 Stages 快照")

    started_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    round = Column(Integer, nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)
    created_by = Column(BigInteger, nullable=True)
