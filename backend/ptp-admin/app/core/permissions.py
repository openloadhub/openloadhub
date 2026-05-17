"""
RBAC 权限矩阵

定义"资源 × 动作 × 角色"权限映射。
使用 require_permission(resource, action) 作为 FastAPI 依赖注入。
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import ActorPrincipal, get_actor_principal, get_db
from app.core.config import settings
from app.models.user import UserRole, UserStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 权限矩阵：key = "resource.action", value = 允许的角色集合
# 特殊值：
#   None → 所有已登录用户都可访问
#   角色集合 → 只有这些角色可访问
#
# 策略说明：
#   1. require_permission() 作为入口守卫，基于角色拦截
#   2. 服务层（task_service/run_service）内的 owner 校验是额外业务约束
#      例如：task.edit 允许 TESTER 角色，但服务层会进一步检查 owner
#   3. task.delete 只允许 ADMIN 角色，服务层 owner 校验不能覆盖此限制
# ---------------------------------------------------------------------------

ALL_ROLES = {UserRole.ADMIN, UserRole.MANAGER, UserRole.TESTER, UserRole.VIEWER}
WRITE_ROLES = {UserRole.ADMIN, UserRole.MANAGER, UserRole.TESTER}
APPROVER_ROLES = {UserRole.ADMIN, UserRole.MANAGER}
ADMIN_ONLY_ROLES = {UserRole.ADMIN}
COMPATIBILITY_ONLY_PERMISSION_KEYS = {
    "approval.view",
    "approval.submit",
    "approval.process",
}

PERMISSION_MAP: dict[str, Optional[set[UserRole]]] = {
    # ---------- tasks ----------
    "task.view": None,  # 所有人可查看
    "task.create": WRITE_ROLES,
    "task.edit": WRITE_ROLES,  # owner 校验在业务层
    "task.delete": {UserRole.ADMIN},
    # ---------- runs ----------
    "run.view": None,
    "run.create": WRITE_ROLES,
    "run.stop": WRITE_ROLES,
    # ---------- scripts ----------
    "script.view": None,
    "script.upload": WRITE_ROLES,
    "script.delete": {UserRole.ADMIN},
    # ---------- plans ----------
    "plan.view": None,
    "plan.create": WRITE_ROLES,
    "plan.edit": WRITE_ROLES,
    "plan.delete": {UserRole.ADMIN},
    "plan.execute": {UserRole.ADMIN, UserRole.MANAGER, UserRole.TESTER},
    # ---------- approvals (deprecated / compatibility_only) ----------
    # 主应用入口已卸载 approval / approval-rule API。
    # 这里保留权限 key 仅为兼容代码、fixture 与 focused test 服务，
    # 不代表审批流仍是 Solo 顾问版正式能力。
    "approval.view": None,
    "approval.submit": WRITE_ROLES,
    "approval.process": APPROVER_ROLES,
    # ---------- reports ----------
    "report.view": None,
    "report.download": None,
    "report.delete": {UserRole.ADMIN},
    # ---------- agents ----------
    "agent.view": ADMIN_ONLY_ROLES,
    "agent.stop": ADMIN_ONLY_ROLES,
    # ---------- notifications ----------
    "notification.manage": {UserRole.ADMIN, UserRole.MANAGER},
    # ---------- admin ----------
    "audit.view": {UserRole.ADMIN, UserRole.MANAGER},
    "self_apm.view": ADMIN_ONLY_ROLES,
    "user.manage": {UserRole.ADMIN},
}


def check_permission(
    principal: ActorPrincipal,
    resource: str,
    action: str,
) -> bool:
    """
    检查用户是否有权限执行某个操作。

    注意：此函数仅检查角色权限，不检查资源 owner。
    Owner 校验由服务层（task_service/run_service）在业务逻辑中单独处理。

    Args:
        principal: 当前用户身份
        resource: 资源名称（如 "task", "run"）
        action: 动作名称（如 "create", "delete"）

    Returns:
        True 表示有权限
    """
    if principal.is_superuser:
        return True

    key = f"{resource}.{action}"
    allowed_roles = PERMISSION_MAP.get(key)

    # 未注册的权限 key → 默认拒绝
    if key not in PERMISSION_MAP:
        logger.warning("Unknown permission key: %s", key)
        return False

    # None → 所有已登录用户都可访问
    if allowed_roles is None:
        return True

    # 角色匹配
    if principal.role in allowed_roles:
        return True

    return False


def _principal_with_persisted_role(
    principal: ActorPrincipal,
    db: Session,
) -> ActorPrincipal:
    """Use the current DB role when a signed token only carries a user id."""
    if principal.user_id is None:
        return principal

    from app.models.user import User

    user = db.get(User, principal.user_id)
    if user is None:
        if settings.TESTING and principal.role is not None:
            return principal
        return ActorPrincipal(user_id=principal.user_id)

    if not user.is_active or user.status != UserStatus.ACTIVE:
        return ActorPrincipal(user_id=principal.user_id)

    return ActorPrincipal(
        user_id=user.id,
        role=user.role,
        is_superuser=bool(user.is_superuser),
    )


def require_permission(resource: str, action: str):
    """
    FastAPI 依赖注入工厂：检查当前用户对指定资源的操作权限。

    用法：
        @router.delete("/tasks/{task_id}", dependencies=[Depends(require_permission("task", "delete"))])

    或在函数参数中：
        def delete_task(..., _perm=Depends(require_permission("task", "delete"))):
    """

    def _checker(
        principal: ActorPrincipal = Depends(get_actor_principal),
        db: Session = Depends(get_db),
    ) -> ActorPrincipal:
        effective_principal = _principal_with_persisted_role(principal, db)
        if not check_permission(effective_principal, resource, action):
            from app.core.audit import log_audit_event

            log_audit_event(
                action="authz.write_check",
                principal=effective_principal,
                resource_type="authorization",
                outcome="forbidden",
                detail=f"Forbidden: insufficient permission for {resource}.{action}",
                db=db,
            )
            raise HTTPException(
                status_code=403,
                detail=f"Forbidden: insufficient permission for {resource}.{action}",
            )
        return effective_principal

    return _checker


def ensure_admin_role_or_raise(
    principal: ActorPrincipal,
    *,
    permission_hint: str,
    db: Optional[Session] = None,
) -> None:
    if principal.is_superuser or principal.role == UserRole.ADMIN:
        return

    from app.core.audit import log_audit_event

    detail = f"Forbidden: insufficient permission for {permission_hint}"
    log_audit_event(
        action="authz.admin_check",
        principal=principal,
        resource_type="authorization",
        outcome="forbidden",
        detail=detail,
        db=db,
    )
    raise HTTPException(status_code=403, detail=detail)
