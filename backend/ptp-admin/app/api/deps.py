import os
import ipaddress
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import Depends, Header, HTTPException, Request, status
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.audit import log_audit_event
from app.models.user import UserRole


def get_db() -> Session:
    """FastAPI 依赖：数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@dataclass(frozen=True)
class ActorPrincipal:
    user_id: Optional[int] = None
    role: Optional[UserRole] = None
    is_superuser: bool = False


def _decode_bearer_claims(authorization: Optional[str]) -> Optional[dict[str, Any]]:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        if not isinstance(payload, dict):
            return None
        return payload
    except (JWTError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def _parse_positive_int(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _normalize_user_role(value: Any) -> Optional[UserRole]:
    if value is None:
        return None
    if isinstance(value, UserRole):
        return value
    raw = str(value).strip().upper()
    if not raw:
        return None
    try:
        return UserRole(raw)
    except ValueError:
        return None


def _trusted_auth_header_cidrs() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    raw_value = getattr(settings, "TRUSTED_AUTH_HEADER_CIDRS", "") or ""
    for raw_item in raw_value.split(","):
        item = raw_item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return networks


def _should_trust_auth_headers(request: Request) -> bool:
    if getattr(settings, "TESTING", False):
        return True
    if request.client is None or not request.client.host:
        return False
    try:
        client_ip = ipaddress.ip_address(request.client.host)
    except ValueError:
        return False
    return any(client_ip in network for network in _trusted_auth_header_cidrs())


def get_actor_principal(
    request: Request,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_user_role: Optional[str] = Header(default=None, alias="X-User-Role"),
    x_is_superuser: Optional[str] = Header(default=None, alias="X-Is-Superuser"),
) -> ActorPrincipal:
    claims = _decode_bearer_claims(authorization) or {}
    jwt_user_id = (
        _parse_positive_int(str(claims.get("sub")))
        if claims.get("sub") is not None
        else None
    )
    trust_auth_headers = _should_trust_auth_headers(request)
    header_user_id = _parse_positive_int(x_user_id) if trust_auth_headers else None
    user_id = jwt_user_id or header_user_id or None

    role = _normalize_user_role(claims.get("role"))
    if role is None and trust_auth_headers:
        role = _normalize_user_role(x_user_role)

    superuser = _parse_bool(claims.get("is_superuser"))
    if superuser is None and trust_auth_headers:
        superuser = _parse_bool(x_is_superuser)

    return ActorPrincipal(
        user_id=user_id,
        role=role,
        is_superuser=bool(superuser),
    )


def ensure_approver_role_or_raise(
    principal: ActorPrincipal, db: Optional[Session] = None
) -> None:
    allowed_roles = get_configured_approver_roles()
    if principal.is_superuser:
        return
    if principal.role in allowed_roles:
        return
    allowed_roles_text = ",".join(sorted(role.value for role in allowed_roles))
    log_audit_event(
        action="authz.approver_check",
        principal=principal,
        resource_type="authorization",
        outcome="forbidden",
        detail=f"Forbidden: approver role required ({allowed_roles_text})",
        db=db,
    )
    raise HTTPException(
        status_code=403,
        detail=f"Forbidden: approver role required ({allowed_roles_text})",
    )


def ensure_write_role_or_raise(
    principal: ActorPrincipal, db: Optional[Session] = None
) -> None:
    if principal.is_superuser:
        return
    if principal.role is None or principal.role == UserRole.VIEWER:
        log_audit_event(
            action="authz.write_check",
            principal=principal,
            resource_type="authorization",
            outcome="forbidden",
            detail="Forbidden: write access requires an authorized role",
            db=db,
        )
        raise HTTPException(
            status_code=403,
            detail="Forbidden: write access requires an authorized role",
        )


def get_configured_approver_roles() -> set[UserRole]:
    configured = os.getenv("APPROVER_ROLES", "ADMIN,MANAGER")
    roles: set[UserRole] = set()
    for raw_role in configured.split(","):
        normalized = _normalize_user_role(raw_role)
        if normalized is not None:
            roles.add(normalized)
    if roles:
        return roles
    return {UserRole.ADMIN, UserRole.MANAGER}


def get_actor_user_id(
    principal: ActorPrincipal = Depends(get_actor_principal),
) -> int:
    """获取当前调用方用户ID：优先 JWT claims；可信内网注入时回退 X-User-Id。"""
    return principal.user_id
