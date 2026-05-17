from __future__ import annotations

from pathlib import Path
import sys

COMMON_PARENT = Path(__file__).resolve().parents[3]
if COMMON_PARENT.exists():
    sys.path.append(str(COMMON_PARENT))

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, JSON, String, Text
from sqlalchemy.sql import func
from sqlalchemy.types import Enum as SQLEnum

from app.core.database import Base
from common.models.enums import PlanExecType, PlanStatus


class Plan(Base):
    __tablename__ = "olh_plan"
    __table_args__ = {"sqlite_autoincrement": True}

    plan_id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
        comment="计划ID",
    )

    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    status = Column(
        SQLEnum(
            PlanStatus,
            native_enum=False,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        default=PlanStatus.READY,
        index=True,
    )
    exec_type = Column(
        SQLEnum(
            PlanExecType,
            native_enum=False,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        default=PlanExecType.MANUAL,
    )
    cron = Column(String(128), nullable=True)
    scheduled_at = Column(DateTime, nullable=True)
    timezone = Column(String(64), nullable=True)
    domain_type = Column(
        String(32),
        nullable=False,
        default="plan",
        server_default="plan",
        index=True,
    )

    enable_round = Column(Boolean, nullable=True)
    total_round = Column(Integer, nullable=True)

    business_lines = Column(JSON, nullable=True, comment="计划级业务线列表")
    collaborator_ids = Column(JSON, nullable=True, comment="计划级协作人 ID 列表")
    stages = Column(JSON, nullable=False)

    created_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)
    updated_at = Column(DateTime, server_default=func.now())
