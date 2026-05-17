"""
用户管理模型

管理用户账户信息和权限
"""

from sqlalchemy import Column, BigInteger, String, Text, DateTime, Integer, Boolean, Enum as SQLEnum
from sqlalchemy.sql import func
from app.core.database import Base
import enum

class UserRole(enum.Enum):
    """用户角色"""
    ADMIN = "ADMIN"
    MANAGER = "MANAGER"
    TESTER = "TESTER"
    VIEWER = "VIEWER"

class UserStatus(enum.Enum):
    """用户状态"""
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    LOCKED = "LOCKED"

class User(Base):
    """用户模型"""
    __tablename__ = "olh_user"
    __table_args__ = {"sqlite_autoincrement": True}

    id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
        comment="用户ID"
    )
    username = Column(String(100), unique=True, nullable=False, index=True, comment="用户名")
    email = Column(String(255), unique=True, nullable=False, index=True, comment="邮箱")
    full_name = Column(String(255), nullable=False, comment="全名")
    hashed_password = Column(String(255), nullable=False, comment="加密密码")
    role = Column(SQLEnum(UserRole), nullable=False, default=UserRole.TESTER, comment="用户角色")
    status = Column(SQLEnum(UserStatus), nullable=False, default=UserStatus.ACTIVE, index=True, comment="用户状态")

    # 安全信息
    is_superuser = Column(Boolean, default=False, comment="是否超级用户")
    is_active = Column(Boolean, default=True, comment="是否激活")
    last_login_at = Column(DateTime, comment="最后登录时间")
    login_count = Column(Integer, default=0, comment="登录次数")
    failed_login_attempts = Column(Integer, default=0, comment="失败登录次数")

    # 时间戳
    created_at = Column(DateTime, nullable=False, server_default=func.now(), index=True, comment="创建时间")
    updated_at = Column(DateTime, onupdate=func.now(), server_default=func.now(), comment="更新时间")

    def __repr__(self):
        return f"<User(id={self.id}, username={self.username}, role={self.role})>"