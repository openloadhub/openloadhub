from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.sql import func

from app.core.database import Base


class RunBaseline(Base):
    __tablename__ = "olh_run_baseline"
    __table_args__ = (
        UniqueConstraint("scope_type", "scope_key", name="uq_olh_run_baseline_scope"),
        {"sqlite_autoincrement": True},
    )

    baseline_id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
        comment="基线ID",
    )
    scope_type = Column(String(32), nullable=False, index=True, comment="基线作用域类型")
    scope_key = Column(String(255), nullable=False, index=True, comment="基线作用域唯一键")
    task_id = Column(BigInteger, nullable=False, index=True, comment="任务ID")
    env = Column(String(64), nullable=False, index=True, comment="环境")
    protocol = Column(String(32), nullable=True, index=True, comment="协议")
    baseline_run_id = Column(BigInteger, nullable=False, index=True, comment="基线run_id")
    baseline_source = Column(String(32), nullable=False, default="manual", comment="基线来源")
    effective_from = Column(
        DateTime, nullable=False, server_default=func.now(), index=True, comment="生效时间"
    )
    note = Column(Text, nullable=True, comment="备注")
    created_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
