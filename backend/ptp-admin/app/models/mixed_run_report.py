from __future__ import annotations

from pathlib import Path
import sys

from sqlalchemy import BigInteger, Column, DateTime, Integer, JSON, String, Text
from sqlalchemy.sql import func

COMMON_PARENT = Path(__file__).resolve().parents[3]
if COMMON_PARENT.exists():
    sys.path.append(str(COMMON_PARENT))

from app.core.database import Base


class MixedRunReport(Base):
    __tablename__ = "olh_mixed_run_report"
    __table_args__ = {"sqlite_autoincrement": True}

    report_id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
        comment="混压报告ID",
    )
    mixed_run_id = Column(BigInteger, nullable=False, index=True, comment="混压ID")
    round = Column(Integer, nullable=False, default=1, index=True, comment="轮次")
    collection_id = Column(BigInteger, nullable=True, index=True, comment="选中集合ID")
    version = Column(Integer, nullable=False, default=1, comment="报告版本")
    status = Column(String(32), nullable=False, default="pending", index=True)
    summary = Column(Text, nullable=True)
    payload_json = Column(JSON, nullable=True)
    artifact_path = Column(String(500), nullable=True)
    file_size = Column(Integer, nullable=True)
    input_sources = Column(JSON, nullable=True)
    limitations = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    generated_by = Column(BigInteger, nullable=True, index=True)
    generated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)
    updated_at = Column(DateTime, nullable=True, server_default=func.now(), onupdate=func.now())
