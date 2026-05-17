from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

from common.utils.time import to_rfc3339_z


class AuditLogResponse(BaseModel):
    id: int
    action: str
    outcome: str
    actor_id: Optional[int] = None
    actor_role: Optional[str] = None
    actor_superuser: bool
    resource_type: str
    resource_id: Optional[str] = None
    detail: Optional[str] = None
    extra: Optional[dict[str, Any]] = None
    created_at: datetime

    model_config = ConfigDict(
        from_attributes=True,
    )

