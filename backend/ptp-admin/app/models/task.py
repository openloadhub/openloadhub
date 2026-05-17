from __future__ import annotations

from pathlib import Path
import sys

COMMON_PARENT = Path(__file__).resolve().parents[3]
if COMMON_PARENT.exists():
    sys.path.append(str(COMMON_PARENT))

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.sql import func
from sqlalchemy.types import Enum as SQLEnum

from app.core.database import Base
from common.models.enums import EngineType, TaskPattern, TaskStatus


class Task(Base):
    __tablename__ = "olh_task"
    __table_args__ = {"sqlite_autoincrement": True}

    id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
        comment="任务ID",
    )
    name = Column(String(255), nullable=False, comment="任务名称")
    description = Column(Text, comment="任务描述")
    env = Column(String(64), nullable=False, comment="运行环境")
    script_id = Column(BigInteger, nullable=False, index=True, comment="关联脚本ID")

    # 关联关系
    # script = relationship("Script", backref="tasks")
    status = Column(
        SQLEnum(
            TaskStatus,
            native_enum=False,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        default=TaskStatus.DRAFT,
        index=True,
    )
    engine_type = Column(
        SQLEnum(
            EngineType,
            native_enum=False,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        comment="引擎类型",
    )
    task_pattern = Column(
        SQLEnum(
            TaskPattern,
            native_enum=False,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        default=TaskPattern.SCRIPT,
    )
    protocols = Column(JSON, nullable=False, comment="协议类型列表")
    thread_count = Column(Integer, nullable=False, comment="并发线程数")
    duration = Column(Integer, nullable=False, comment="执行时长(秒)")
    ramp_up = Column(Integer, default=0, comment="加压时间(秒)")
    properties = Column(JSON, comment="额外配置参数")
    created_by = Column(BigInteger, comment="创建人ID")
    collaborator_ids = Column(JSON, comment="协作人 ID 列表")
    created_at = Column(
        DateTime, nullable=False, server_default=func.now(), index=True
    )
    updated_at = Column(DateTime, onupdate=func.now(), server_default=func.now())
    last_run_at = Column(DateTime, comment="最近一次执行时间")

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<Task(id={self.id}, name={self.name}, status={self.status})>"
