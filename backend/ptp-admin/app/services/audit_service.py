from __future__ import annotations

from datetime import datetime
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog


class AuditService:
    def __init__(self, db: Session):
        self.db = db

    def list_audit_logs(
        self,
        *,
        page: int,
        page_size: int,
        action: Optional[str] = None,
        outcome: Optional[str] = None,
        actor_id: Optional[int] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
    ) -> Tuple[list[AuditLog], int]:
        query = self.db.query(AuditLog)

        if action:
            query = query.filter(AuditLog.action == action)
        if outcome:
            query = query.filter(AuditLog.outcome == outcome)
        if actor_id is not None:
            query = query.filter(AuditLog.actor_id == actor_id)
        if resource_type:
            query = query.filter(AuditLog.resource_type == resource_type)
        if resource_id:
            query = query.filter(AuditLog.resource_id == resource_id)
        if created_from is not None:
            query = query.filter(AuditLog.created_at >= created_from)
        if created_to is not None:
            query = query.filter(AuditLog.created_at <= created_to)

        total = int(query.count())
        items = (
            query.order_by(AuditLog.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return items, total

