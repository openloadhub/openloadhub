from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.models.plan import Plan
from common.models.enums import PlanStatus


def resolve_plan_preserved_updated_at(plan: Plan) -> datetime | None:
    return getattr(plan, "updated_at", None) or getattr(plan, "created_at", None)


def set_plan_status_without_touching_updated_at(
    db: Session, plan: Plan, next_status: PlanStatus
) -> None:
    preserved_updated_at = resolve_plan_preserved_updated_at(plan)
    values = {Plan.status: next_status}
    if preserved_updated_at is not None:
        values[Plan.updated_at] = preserved_updated_at
    (
        db.query(Plan)
        .filter(Plan.plan_id == plan.plan_id)
        .update(values, synchronize_session=False)
    )
    try:
        set_committed_value(plan, "status", next_status)
        if preserved_updated_at is not None:
            set_committed_value(plan, "updated_at", preserved_updated_at)
    except Exception:
        # 单测里 Plan 常被 MagicMock 代替；保留数据库写入，同时同步本地对象即可。
        setattr(plan, "status", next_status)
        if preserved_updated_at is not None:
            setattr(plan, "updated_at", preserved_updated_at)
