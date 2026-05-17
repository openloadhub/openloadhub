from __future__ import annotations

from pathlib import Path
import sys

from sqlalchemy import BigInteger, Column, DateTime, Integer, JSON, String, Text
from sqlalchemy.sql import func

COMMON_PARENT = Path(__file__).resolve().parents[3]
if COMMON_PARENT.exists():
    sys.path.append(str(COMMON_PARENT))

from app.core.database import Base


class TaskAsset(Base):
    __tablename__ = "olh_task_asset"
    __table_args__ = {"sqlite_autoincrement": True}

    id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
        comment="附件ID",
    )
    task_id = Column(
        BigInteger, nullable=True, index=True, comment="任务ID（未绑定时为空）"
    )
    category = Column(
        String(32), nullable=False, index=True, comment="附件分类：proto/data"
    )
    file_name = Column(String(255), nullable=False, comment="原始文件名")
    file_path = Column(String(500), nullable=False, comment="存储路径")
    file_size = Column(BigInteger, nullable=False, comment="文件大小")
    content_hash = Column(String(64), nullable=True, comment="内容哈希")
    storage_type = Column(
        String(32), nullable=False, default="local", server_default="local"
    )
    compression_type = Column(String(32), nullable=True, comment="上传压缩格式")
    compressed_file_size = Column(BigInteger, nullable=True, comment="压缩包原始大小")
    line_count = Column(BigInteger, nullable=True, comment="文本数据行数")
    ingest_status = Column(
        String(32), nullable=False, default="completed", server_default="completed"
    )
    ingest_error = Column(Text, nullable=True)
    metadata_json = Column(JSON, nullable=True)
    created_by = Column(BigInteger, nullable=True, index=True, comment="创建人")
    created_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)
    updated_at = Column(DateTime, onupdate=func.now(), server_default=func.now())

    def __repr__(self) -> str:  # pragma: no cover
        return f"<TaskAsset(id={self.id}, task_id={self.task_id}, category={self.category}, file_name={self.file_name})>"
