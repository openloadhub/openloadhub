from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import (
    ActorPrincipal,
    ensure_approver_role_or_raise,
    get_actor_principal,
    get_db,
)
from app.schemas.audit import AuditLogResponse
from app.schemas.response import ApiResponse, PageResult
from app.services.audit_service import AuditService

router = APIRouter()


@router.get(
    "/audit-logs",
    response_model=ApiResponse[PageResult[AuditLogResponse]],
    response_model_by_alias=True,
)
def list_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200, alias="pageSize"),
    action: Optional[str] = Query(None),
    outcome: Optional[str] = Query(None),
    actor_id: Optional[int] = Query(None, ge=1),
    resource_type: Optional[str] = Query(None),
    resource_id: Optional[str] = Query(None),
    created_from: Optional[datetime] = Query(None, alias="from"),
    created_to: Optional[datetime] = Query(None, alias="to"),
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(get_actor_principal),
):
    ensure_approver_role_or_raise(actor, db=db)
    service = AuditService(db)
    items, total = service.list_audit_logs(
        page=page,
        page_size=page_size,
        action=action,
        outcome=outcome,
        actor_id=actor_id,
        resource_type=resource_type,
        resource_id=resource_id,
        created_from=created_from,
        created_to=created_to,
    )
    return ApiResponse.success(
        PageResult(items=items, total=total, page=page, page_size=page_size)
    )
