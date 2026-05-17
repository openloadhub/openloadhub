from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.sql import func

from app.core.database import Base


class AuditLog(Base):
    __tablename__ = "olh_audit_log"
    __table_args__ = {"sqlite_autoincrement": True}

    id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
        comment="审计记录ID",
    )
    action = Column(String(128), nullable=False, index=True, comment="动作标识")
    outcome = Column(
        String(32), nullable=False, default="success", index=True, comment="结果"
    )
    actor_id = Column(BigInteger, nullable=True, index=True, comment="操作者ID")
    actor_role = Column(String(64), nullable=True, comment="操作者角色")
    actor_superuser = Column(
        Boolean, nullable=False, default=False, comment="是否超级管理员"
    )
    resource_type = Column(
        String(64), nullable=False, index=True, comment="资源类型"
    )
    resource_id = Column(String(128), nullable=True, index=True, comment="资源ID")
    detail = Column(Text, nullable=True, comment="补充说明")
    extra = Column(JSON, nullable=True, comment="扩展上下文")
    created_at = Column(
        DateTime, nullable=False, server_default=func.now(), index=True, comment="创建时间"
    )

    def __repr__(self) -> str:
        return (
            f"<AuditLog(id={self.id}, action={self.action}, outcome={self.outcome}, "
            f"resource_type={self.resource_type}, resource_id={self.resource_id})>"
        )

