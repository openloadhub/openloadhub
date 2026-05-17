from __future__ import annotations

from pathlib import Path
import sys

COMMON_PARENT = Path(__file__).resolve().parents[3]
if COMMON_PARENT.exists():
    sys.path.append(str(COMMON_PARENT))

from sqlalchemy import BigInteger, Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from app.core.database import Base


class TaskVersionRecord(Base):
    __tablename__ = "olh_task_version_history"
    __table_args__ = {"sqlite_autoincrement": True}

    id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
        comment="主键",
    )
    task_id = Column(BigInteger, nullable=False, index=True, comment="任务ID")
    version = Column(String(50), nullable=False, comment="版本号")
    created_by = Column(BigInteger, nullable=True, comment="修改人 ID")
    task_snapshot = Column(Text, nullable=False, comment="任务快照(JSON)")
    script_content = Column(Text, nullable=True, comment="脚本正文快照")
    created_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)
    updated_at = Column(DateTime, onupdate=func.now(), server_default=func.now())

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<TaskVersionRecord(id={self.id}, task_id={self.task_id}, version={self.version})>"
