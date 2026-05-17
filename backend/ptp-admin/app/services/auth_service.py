"""
认证业务服务层
"""

import warnings
from typing import Optional, Dict, Any
from datetime import datetime, timedelta, timezone
from jose import JWTError, jwt
from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.models.user import User, UserStatus, UserRole
from app.schemas.auth import UserCreate, Token, UserResponse
from app.core.config import settings

with warnings.catch_warnings():
    # Python 3.13 deprecates stdlib `crypt`, but passlib still imports it internally
    # when building CryptContext. Keep the suppression local to this module import.
    warnings.filterwarnings(
        "ignore",
        message="'crypt' is deprecated and slated for removal in Python 3.13",
        category=DeprecationWarning,
        module="passlib.utils",
    )
    from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["sha256_crypt"], deprecated="auto")


class AuthService:
    """认证业务服务"""

    def __init__(self, db: Session):
        self.db = db

    def verify_password(self, plain_password: str, hashed_password: str) -> bool:
        """验证密码"""
        return pwd_context.verify(plain_password, hashed_password)

    def get_password_hash(self, password: str) -> str:
        """生成密码哈希"""
        # bcrypt限制密码长度为72字节
        if len(password.encode("utf-8")) > 72:
            raise ValueError("Password cannot be longer than 72 bytes")
        return pwd_context.hash(password)

    def get_user_by_username(self, username: str) -> Optional[User]:
        """根据用户名获取用户"""
        return self.db.query(User).filter(User.username == username).first()

    def get_user_by_email(self, email: str) -> Optional[User]:
        """根据邮箱获取用户"""
        return self.db.query(User).filter(User.email == email).first()

    def create_user(self, user_in: UserCreate) -> User:
        """创建用户"""
        # 检查用户名是否已存在
        if self.get_user_by_username(user_in.username):
            raise ValueError("Username already registered")

        # 检查邮箱是否已存在
        if self.get_user_by_email(user_in.email):
            raise ValueError("Email already registered")

        # 创建用户
        hashed_password = self.get_password_hash(user_in.password)
        user_data = user_in.model_dump()
        user_data["hashed_password"] = hashed_password
        # 移除明文密码字段
        user_data.pop("password", None)

        user = User(**user_data)
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        return user

    def authenticate_user(self, username: str, password: str) -> Optional[User]:
        """认证用户"""
        user = self.get_user_by_username(username)
        if not user:
            return None

        if not self.verify_password(password, user.hashed_password):
            return None

        if not user.is_active or user.status != UserStatus.ACTIVE:
            return None

        # 更新登录信息
        user.last_login_at = datetime.now(timezone.utc)
        user.login_count += 1
        user.failed_login_attempts = 0
        self.db.commit()

        return user

    def create_access_token(
        self,
        user_id: int,
        role: Optional[UserRole | str] = None,
        is_superuser: bool = False,
    ) -> str:
        """创建访问令牌"""
        expire = datetime.now(timezone.utc) + timedelta(
            seconds=settings.ACCESS_TOKEN_EXPIRE_SECONDS
        )
        to_encode = {"sub": str(user_id), "exp": expire}
        if role is not None:
            to_encode["role"] = role.value if isinstance(role, UserRole) else str(role)
        if is_superuser:
            to_encode["is_superuser"] = True
        encoded_jwt = jwt.encode(
            to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM
        )
        return encoded_jwt

    def verify_token(self, token: str) -> Optional[int]:
        """验证令牌"""
        try:
            payload = jwt.decode(
                token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
            )
            user_id: str = payload.get("sub")
            if user_id is None:
                return None
            return int(user_id)
        except (JWTError, ValueError):
            return None

    def get_user_by_id(self, user_id: int) -> Optional[User]:
        """根据ID获取用户"""
        return self.db.query(User).filter(User.id == user_id).first()

    def update_user(self, user_id: int, update_data: Dict[str, Any]) -> Optional[User]:
        """更新用户"""
        user = self.get_user_by_id(user_id)
        if not user:
            return None

        for key, value in update_data.items():
            if key == "password":
                user.hashed_password = self.get_password_hash(value)
            else:
                setattr(user, key, value)

        self.db.commit()
        self.db.refresh(user)
        return user

    def change_password(
        self, user_id: int, current_password: str, new_password: str
    ) -> bool:
        """修改密码"""
        user = self.get_user_by_id(user_id)
        if not user:
            return False

        if not self.verify_password(current_password, user.hashed_password):
            return False

        user.hashed_password = self.get_password_hash(new_password)
        self.db.commit()
        return True

    def is_admin(self, user: User) -> bool:
        """检查是否为管理员"""
        return user.role == UserRole.ADMIN or user.is_superuser

    def can_approve(self, user: User) -> bool:
        """检查是否可以审批"""
        return user.role in [UserRole.ADMIN, UserRole.MANAGER]

    def to_user_response(self, user: User) -> UserResponse:
        """转换为响应模型"""
        return UserResponse.model_validate(user)
