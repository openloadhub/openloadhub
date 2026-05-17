from __future__ import annotations

from pathlib import Path
import sys

COMMON_PARENT = Path(__file__).resolve().parents[3]
if COMMON_PARENT.exists():
    sys.path.append(str(COMMON_PARENT))

from sqlalchemy import BigInteger, Column, DateTime, Float, Integer, JSON, String, Text
from sqlalchemy.sql import func
from sqlalchemy.types import Enum as SQLEnum

from app.core.database import Base
from common.models.enums import EngineType, RunStatus


class Run(Base):
    __tablename__ = "olh_run"
    __table_args__ = {"sqlite_autoincrement": True}

    run_id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
        comment="执行记录ID",
    )

    task_id = Column(BigInteger, nullable=False, index=True, comment="任务ID")
    task_name = Column(String(255), nullable=True, comment="任务名（可选冗余）")

    engine_type = Column(
        SQLEnum(
            EngineType,
            native_enum=False,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        comment="引擎类型",
    )
    protocol = Column(String(32), nullable=True, comment="协议类型（可选）")
    env = Column(String(64), nullable=False, comment="运行环境")

    run_status = Column(
        SQLEnum(
            RunStatus,
            native_enum=False,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        default=RunStatus.PREPARING,
        index=True,
    )
    run_status_detail = Column(String(64), nullable=True, comment="细分阶段（可选）")

    started_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    duration_seconds = Column(Integer, nullable=True)

    params = Column(JSON, nullable=True, comment="本次运行参数快照")

    total_requests = Column(Integer, nullable=True)
    success_rate = Column(Float, nullable=True)
    error_rate = Column(Float, nullable=True)
    avg_rt_ms = Column(Float, nullable=True)
    p95_rt_ms = Column(Float, nullable=True)
    p99_rt_ms = Column(Float, nullable=True)
    rps = Column(Float, nullable=True)

    stop_reason = Column(Text, nullable=True)

    idempotency_key = Column(String(128), nullable=True, unique=True, index=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
