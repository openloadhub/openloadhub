"""
认证 API 路由
"""

from typing import Optional
from types import SimpleNamespace
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.audit import log_audit_event
from app.core.config import settings
from app.schemas.auth import (
    UserCreate,
    UserResponse,
    LoginRequest,
    Token,
    ChangePasswordRequest,
)
from app.services.auth_service import AuthService
from app.models.user import User

router = APIRouter()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


def _anonymous_principal():
    return SimpleNamespace(user_id=None, role=None, is_superuser=False)


def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> User:
    """获取当前用户"""
    auth_service = AuthService(db)
    user_id = auth_service.verify_token(token)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = auth_service.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def get_current_active_user(current_user: User = Depends(get_current_user)) -> User:
    """获取当前激活用户"""
    if not current_user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
    return current_user


@router.post("/register", response_model=UserResponse, status_code=201)
@router.post("/auth/register", response_model=UserResponse, status_code=201)
def register(user_in: UserCreate, db: Session = Depends(get_db)):
    """用户注册"""
    if not settings.ALLOW_SELF_REGISTER:
        log_audit_event(
            action="auth.register",
            principal=_anonymous_principal(),
            resource_type="user",
            outcome="forbidden",
            detail="Self registration is disabled; use DEFAULT_ADMIN bootstrap or an existing admin account.",
            extra={"username": user_in.username, "role": str(user_in.role)},
            db=db,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Self registration is disabled; use DEFAULT_ADMIN bootstrap or an existing admin account.",
        )
    auth_service = AuthService(db)
    try:
        user = auth_service.create_user(user_in)
        log_audit_event(
            action="auth.register",
            principal=SimpleNamespace(
                user_id=user.id,
                role=user.role,
                is_superuser=bool(user.is_superuser),
            ),
            resource_type="user",
            resource_id=user.id,
            extra={"username": user.username},
            db=db,
        )
        return auth_service.to_user_response(user)
    except ValueError as e:
        log_audit_event(
            action="auth.register",
            principal=_anonymous_principal(),
            resource_type="user",
            outcome="failed",
            detail=str(e),
            extra={"username": user_in.username, "role": str(user_in.role)},
            db=db,
        )
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/login", response_model=Token)
@router.post("/auth/login", response_model=Token)
def login(login_in: LoginRequest, db: Session = Depends(get_db)):
    """用户登录"""
    auth_service = AuthService(db)
    user = auth_service.authenticate_user(login_in.username, login_in.password)
    if not user:
        log_audit_event(
            action="auth.login",
            principal=_anonymous_principal(),
            resource_type="user",
            outcome="failed",
            detail="Incorrect username or password",
            extra={"username": login_in.username},
            db=db,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = auth_service.create_access_token(
        user.id,
        role=user.role,
        is_superuser=bool(user.is_superuser),
    )
    log_audit_event(
        action="auth.login",
        principal=SimpleNamespace(
            user_id=user.id,
            role=user.role,
            is_superuser=bool(user.is_superuser),
        ),
        resource_type="user",
        resource_id=user.id,
        extra={"username": user.username},
        db=db,
    )
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": 3600,
        "user": auth_service.to_user_response(user),
    }


@router.get("/me", response_model=UserResponse)
@router.get("/auth/me", response_model=UserResponse)
def get_current_user_info(current_user: User = Depends(get_current_active_user)):
    """获取当前用户信息"""
    return current_user


@router.put("/me/password", status_code=200)
@router.put("/auth/me/password", status_code=200)
def change_password(
    password_in: ChangePasswordRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """修改当前用户密码"""
    auth_service = AuthService(db)
    success = auth_service.change_password(
        current_user.id, password_in.current_password, password_in.new_password
    )
    if not success:
        log_audit_event(
            action="auth.password_change",
            principal=SimpleNamespace(
                user_id=current_user.id,
                role=current_user.role,
                is_superuser=bool(current_user.is_superuser),
            ),
            resource_type="user",
            resource_id=current_user.id,
            outcome="failed",
            detail="Incorrect password",
            db=db,
        )
        raise HTTPException(status_code=400, detail="Incorrect password")
    log_audit_event(
        action="auth.password_change",
        principal=SimpleNamespace(
            user_id=current_user.id,
            role=current_user.role,
            is_superuser=bool(current_user.is_superuser),
        ),
        resource_type="user",
        resource_id=current_user.id,
        db=db,
    )
    return {"message": "Password changed successfully"}


@router.post("/logout", status_code=200)
@router.post("/auth/logout", status_code=200)
def logout():
    """用户登出（客户端删除令牌即可）"""
    return {"message": "Successfully logged out"}


@router.post("/refresh", response_model=Token)
@router.post("/auth/refresh", response_model=Token)
def refresh_token(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """刷新访问令牌"""
    auth_service = AuthService(db)
    access_token = auth_service.create_access_token(
        current_user.id,
        role=current_user.role,
        is_superuser=bool(current_user.is_superuser),
    )
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": 3600,
        "user": auth_service.to_user_response(current_user),
    }
