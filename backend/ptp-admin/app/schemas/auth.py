"""
认证相关的 Pydantic 模型
"""

from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, EmailStr
from datetime import datetime
from app.models.user import UserRole, UserStatus

class UserBase(BaseModel):
    """用户基础模型"""
    username: str = Field(..., min_length=3, max_length=100, description="用户名")
    email: EmailStr = Field(..., description="邮箱")
    full_name: str = Field(..., min_length=1, max_length=255, description="全名")
    role: UserRole = Field(UserRole.TESTER, description="用户角色")

class UserCreate(UserBase):
    """创建用户请求"""
    password: str = Field(..., min_length=8, max_length=72, description="密码")

class UserUpdate(BaseModel):
    """更新用户请求"""
    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    role: Optional[UserRole] = None
    status: Optional[UserStatus] = None

class UserResponse(UserBase):
    """用户响应模型"""
    id: int
    status: UserStatus
    is_superuser: bool
    is_active: bool
    last_login_at: Optional[datetime] = None
    login_count: int
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

class LoginRequest(BaseModel):
    """登录请求"""
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")

class Token(BaseModel):
    """访问令牌"""
    access_token: str = Field(..., description="访问令牌")
    token_type: str = Field(..., description="令牌类型")
    expires_in: int = Field(..., description="过期时间(秒)")
    user: UserResponse = Field(..., description="用户信息")

class TokenData(BaseModel):
    """令牌数据"""
    user_id: Optional[int] = None

class ChangePasswordRequest(BaseModel):
    """修改密码请求"""
    current_password: str = Field(..., description="当前密码")
    new_password: str = Field(..., min_length=8, max_length=72, description="新密码")