from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import (
    ActorPrincipal,
    ensure_write_role_or_raise,
    get_actor_principal,
    get_db,
)
from app.schemas.plan import (
    PlanCreate,
    PlanExecuteRequest,
    PlanExecuteResponse,
    PlanResponse,
    PlanUpdate,
)
from app.schemas.response import ApiResponse, PageResult
from app.services.plan_service import PlanService
from common.models.enums import PlanExecType, PlanStatus
from app.core.audit import log_audit_event

router = APIRouter()


@router.get(
    "/plans",
    response_model=ApiResponse[PageResult[PlanResponse]],
    response_model_by_alias=True,
)
def list_plans(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100, alias="pageSize"),
    plan_id: Optional[int] = Query(None, ge=1),
    name: Optional[str] = Query(None),
    status: Optional[PlanStatus] = Query(None),
    exec_type: Optional[PlanExecType] = Query(None),
    created_by: Optional[int] = Query(None, ge=1),
    created_by_name: Optional[str] = Query(None, min_length=1),
    participant_id: Optional[int] = Query(None, ge=1),
    business_line: Optional[str] = Query(None, min_length=1),
    env: Optional[str] = Query(None, min_length=1),
    engine_type: Optional[str] = Query(None, min_length=1),
    round_mode: Optional[str] = Query(None, min_length=1),
    time_field: Optional[str] = Query(None, min_length=1),
    created_from: Optional[datetime] = Query(None, alias="from"),
    created_to: Optional[datetime] = Query(None, alias="to"),
    db: Session = Depends(get_db),
):
    service = PlanService(db)
    plans, total = service.list_plans(
        page=page,
        page_size=page_size,
        plan_id=plan_id,
        name=name,
        status=status,
        exec_type=exec_type,
        created_by=created_by,
        created_by_name=created_by_name,
        participant_id=participant_id,
        business_line=business_line,
        env=env,
        engine_type=engine_type,
        round_mode=round_mode,
        time_field=time_field,
        created_from=created_from,
        created_to=created_to,
    )
    return ApiResponse.success(
        PageResult(items=plans, total=total, page=page, page_size=page_size)
    )


@router.post(
    "/plans", response_model=ApiResponse[PlanResponse], response_model_by_alias=True
)
def create_plan(
    plan_in: PlanCreate,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(get_actor_principal),
):
    ensure_write_role_or_raise(actor, db=db)
    service = PlanService(db)
    plan = service.create_plan(plan_in, user_id=actor.user_id)
    log_audit_event(
        action="plan.create",
        principal=actor,
        resource_type="plan",
        resource_id=plan.plan_id,
        extra={"name": plan.name},
        db=db,
    )
    return ApiResponse.success(plan)


@router.get(
    "/plans/{plan_id}",
    response_model=ApiResponse[PlanResponse],
    response_model_by_alias=True,
)
def get_plan(plan_id: int, db: Session = Depends(get_db)):
    service = PlanService(db)
    plan = service.get_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    return ApiResponse.success(plan)


@router.put(
    "/plans/{plan_id}",
    response_model=ApiResponse[PlanResponse],
    response_model_by_alias=True,
)
def update_plan(
    plan_id: int,
    plan_in: PlanUpdate,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(get_actor_principal),
):
    ensure_write_role_or_raise(actor, db=db)
    service = PlanService(db)
    try:
        plan = service.update_plan(plan_id, plan_in, user_id=actor.user_id)
    except PermissionError as exc:
        log_audit_event(
            action="plan.update",
            principal=actor,
            resource_type="plan",
            resource_id=plan_id,
            outcome="forbidden",
            detail=str(exc),
            db=db,
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not plan:
        log_audit_event(
            action="plan.update",
            principal=actor,
            resource_type="plan",
            resource_id=plan_id,
            outcome="failed",
            detail="plan_not_found",
            db=db,
        )
        raise HTTPException(status_code=404, detail="Plan not found")
    log_audit_event(
        action="plan.update",
        principal=actor,
        resource_type="plan",
        resource_id=plan.plan_id,
        db=db,
    )
    return ApiResponse.success(plan)


@router.delete(
    "/plans/{plan_id}", response_model=ApiResponse[None], response_model_by_alias=True
)
def delete_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(get_actor_principal),
):
    ensure_write_role_or_raise(actor, db=db)
    service = PlanService(db)
    try:
        deleted = service.delete_plan(plan_id, user_id=actor.user_id)
    except PermissionError as exc:
        log_audit_event(
            action="plan.delete",
            principal=actor,
            resource_type="plan",
            resource_id=plan_id,
            outcome="forbidden",
            detail=str(exc),
            db=db,
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        log_audit_event(
            action="plan.delete",
            principal=actor,
            resource_type="plan",
            resource_id=plan_id,
            outcome="failed",
            detail=str(exc),
            db=db,
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        log_audit_event(
            action="plan.delete",
            principal=actor,
            resource_type="plan",
            resource_id=plan_id,
            outcome="failed",
            detail="plan_not_found",
            db=db,
        )
        raise HTTPException(status_code=404, detail="Plan not found")
    log_audit_event(
        action="plan.delete",
        principal=actor,
        resource_type="plan",
        resource_id=plan_id,
        db=db,
    )
    return ApiResponse.success(None)


@router.post(
    "/plans/{plan_id}/copy",
    response_model=ApiResponse[PlanResponse],
    response_model_by_alias=True,
)
def copy_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(get_actor_principal),
):
    ensure_write_role_or_raise(actor, db=db)
    service = PlanService(db)
    try:
        copied = service.copy_plan(plan_id, user_id=actor.user_id)
    except PermissionError as exc:
        log_audit_event(
            action="plan.copy",
            principal=actor,
            resource_type="plan",
            resource_id=plan_id,
            outcome="forbidden",
            detail=str(exc),
            db=db,
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not copied:
        log_audit_event(
            action="plan.copy",
            principal=actor,
            resource_type="plan",
            resource_id=plan_id,
            outcome="failed",
            detail="plan_not_found",
            db=db,
        )
        raise HTTPException(status_code=404, detail="Plan not found")
    log_audit_event(
        action="plan.copy",
        principal=actor,
        resource_type="plan",
        resource_id=copied.plan_id,
        extra={"source_plan_id": plan_id, "name": copied.name},
        db=db,
    )
    return ApiResponse.success(copied)


@router.post(
    "/plans/{plan_id}/execute",
    response_model=ApiResponse[PlanExecuteResponse],
    response_model_by_alias=True,
)
def execute_plan(
    plan_id: int,
    body: PlanExecuteRequest = PlanExecuteRequest(),
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(get_actor_principal),
):
    ensure_write_role_or_raise(actor, db=db)
    service = PlanService(db)
    try:
        result = service.execute_plan(
            plan_id,
            user_id=actor.user_id,
            start_type=body.start_type,
        )
    except PermissionError as exc:
        log_audit_event(
            action="plan.execute",
            principal=actor,
            resource_type="plan",
            resource_id=plan_id,
            outcome="forbidden",
            detail=str(exc),
            db=db,
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not result:
        log_audit_event(
            action="plan.execute",
            principal=actor,
            resource_type="plan",
            resource_id=plan_id,
            outcome="failed",
            detail="plan_not_found",
            db=db,
        )
        raise HTTPException(status_code=404, detail="Plan not found")
    log_audit_event(
        action="plan.execute",
        principal=actor,
        resource_type="plan",
        resource_id=plan_id,
        extra={"plan_run_id": result.plan_run_id},
        db=db,
    )
    return ApiResponse.success(result)
