from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.deps import (
    ActorPrincipal,
    ensure_write_role_or_raise,
    get_actor_principal,
    get_db,
)
from app.models.plan import Plan
from app.models.plan_run import PlanRun
from app.schemas.plan import (
    PlanRunDetailResponse,
    PlanRunListItem,
    PlanRunResponse,
)
from app.schemas.response import ApiResponse, PageResult
from app.services.plan_service import PlanService
from common.models.enums import PlanRunStatus
from app.core.audit import log_audit_event

router = APIRouter()
MIXED_RUN_PLAN_DOMAIN = "mixed_run"


def _is_mixed_backing_plan_run(db: Session, plan_run_id: int) -> bool:
    plan_run = db.query(PlanRun).filter(PlanRun.plan_run_id == plan_run_id).first()
    if plan_run is None:
        return False
    plan = db.query(Plan).filter(Plan.plan_id == plan_run.plan_id).first()
    return bool(
        plan
        and str(getattr(plan, "domain_type", "plan") or "plan") == MIXED_RUN_PLAN_DOMAIN
    )


@router.get(
    "/plan-runs",
    response_model=ApiResponse[PageResult[PlanRunListItem]],
    response_model_by_alias=True,
)
def list_plan_runs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100, alias="pageSize"),
    plan_run_id: Optional[int] = Query(None, ge=1),
    plan_id: Optional[int] = Query(None, ge=1),
    plan_name: Optional[str] = Query(None),
    business_line: Optional[str] = Query(None, min_length=1),
    env: Optional[str] = Query(None, min_length=1),
    engine_type: Optional[str] = Query(None, min_length=1),
    round_mode: Optional[str] = Query(None, min_length=1),
    status: Optional[PlanRunStatus] = Query(None),
    created_by: Optional[int] = Query(None, ge=1),
    created_by_name: Optional[str] = Query(None, min_length=1),
    started_from: Optional[datetime] = Query(None, alias="from"),
    started_to: Optional[datetime] = Query(None, alias="to"),
    db: Session = Depends(get_db),
):
    service = PlanService(db)
    runs, total = service.list_plan_runs(
        page=page,
        page_size=page_size,
        plan_run_id=plan_run_id,
        plan_id=plan_id,
        plan_name=plan_name,
        business_line=business_line,
        env=env,
        engine_type=engine_type,
        round_mode=round_mode,
        status=status,
        created_by=created_by,
        created_by_name=created_by_name,
        started_from=started_from,
        started_to=started_to,
    )
    return ApiResponse.success(
        PageResult(items=runs, total=total, page=page, page_size=page_size)
    )


@router.get(
    "/plan-runs/{plan_run_id}",
    response_model=ApiResponse[PlanRunDetailResponse],
    response_model_by_alias=True,
)
def get_plan_run(
    plan_run_id: int,
    round: Optional[int] = Query(None, ge=1),
    collection_id: Optional[int] = Query(None, ge=1),
    db: Session = Depends(get_db),
):
    if _is_mixed_backing_plan_run(db, plan_run_id):
        raise HTTPException(status_code=404, detail="PlanRun not found")
    service = PlanService(db)
    plan_run = service.get_plan_run(
        plan_run_id,
        selected_round=round,
        selected_collection_id=collection_id,
    )
    if not plan_run:
        raise HTTPException(status_code=404, detail="PlanRun not found")
    return ApiResponse.success(plan_run)


@router.get("/plan-runs/{plan_run_id}/report-manifest")
def download_plan_run_report_manifest(
    plan_run_id: int,
    round: Optional[int] = Query(None, ge=1),
    collection_id: Optional[int] = Query(None, ge=1),
    db: Session = Depends(get_db),
):
    if _is_mixed_backing_plan_run(db, plan_run_id):
        raise HTTPException(status_code=404, detail="PlanRun not found")
    service = PlanService(db)
    payload = service.build_plan_run_report_manifest(
        plan_run_id=plan_run_id,
        selected_round=round,
        selected_collection_id=collection_id,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="PlanRun not found")
    filename, content = payload
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/plan-runs/{plan_run_id}/stop",
    response_model=ApiResponse[PlanRunResponse],
    response_model_by_alias=True,
)
def stop_plan_run(
    plan_run_id: int,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(get_actor_principal),
):
    ensure_write_role_or_raise(actor, db=db)
    if _is_mixed_backing_plan_run(db, plan_run_id):
        raise HTTPException(status_code=404, detail="PlanRun not found")
    service = PlanService(db)
    try:
        plan_run = service.stop_plan_run(plan_run_id, user_id=actor.user_id)
    except PermissionError as exc:
        log_audit_event(
            action="plan_run.stop",
            principal=actor,
            resource_type="plan_run",
            resource_id=plan_run_id,
            outcome="forbidden",
            detail=str(exc),
            db=db,
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not plan_run:
        log_audit_event(
            action="plan_run.stop",
            principal=actor,
            resource_type="plan_run",
            resource_id=plan_run_id,
            outcome="failed",
            detail="plan_run_not_found",
            db=db,
        )
        raise HTTPException(status_code=404, detail="PlanRun not found")
    log_audit_event(
        action="plan_run.stop",
        principal=actor,
        resource_type="plan_run",
        resource_id=plan_run.plan_run_id,
        db=db,
    )
    return ApiResponse.success(plan_run)
