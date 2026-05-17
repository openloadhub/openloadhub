from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.audit_log import AuditLog

logger = logging.getLogger("ptp.audit")


def log_audit_event(
    *,
    action: str,
    principal: Any,
    resource_type: str,
    resource_id: Optional[Any] = None,
    outcome: str = "success",
    detail: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
    db: Optional[Session] = None,
    persist: bool = True,
) -> None:
    actor_id = getattr(principal, "user_id", None)
    raw_role = getattr(principal, "role", None)
    actor_role = getattr(raw_role, "value", None) or (str(raw_role) if raw_role else "")
    actor_superuser = bool(getattr(principal, "is_superuser", False))
    extra_payload = dict(extra) if extra else None

    payload: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "outcome": outcome,
        "actor_id": actor_id,
        "actor_role": actor_role,
        "actor_superuser": actor_superuser,
        "resource_type": resource_type,
        "resource_id": resource_id,
    }
    if detail:
        payload["detail"] = detail
    if extra_payload:
        payload["extra"] = extra_payload
    logger.info("audit_event %s", json.dumps(payload, ensure_ascii=False, default=str))

    if not persist:
        return

    close_session = db is None
    session = db or SessionLocal()
    try:
        session.add(
            AuditLog(
                action=action,
                outcome=outcome,
                actor_id=int(actor_id) if actor_id is not None else None,
                actor_role=actor_role or None,
                actor_superuser=actor_superuser,
                resource_type=resource_type,
                resource_id=str(resource_id) if resource_id is not None else None,
                detail=detail,
                extra=extra_payload,
            )
        )
        session.commit()
    except Exception as exc:  # pragma: no cover - 容错兜底
        session.rollback()
        logger.warning("audit_event_persist_failed action=%s error=%s", action, exc)
    finally:
        if close_session:
            session.close()
