from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import (
    ActorPrincipal,
    get_actor_principal,
    get_db,
)
from app.core.permissions import require_permission
from common.models.enums import EngineType, RunStatus, TaskStatus
from common.models.enums import TaskPattern
from app.schemas.response import ApiResponse, PageResult
from app.schemas.task import (
    TaskBatchStopRequest,
    TaskBatchStopResponse,
    TaskCreate,
    TaskLastRunParamsResponse,
    TaskPrepareRunResponse,
    TaskResponse,
    TaskScriptCompareResponse,
    TaskSummaryResponse,
    TaskVersionDetailResponse,
    TaskUpdate,
    TaskVersionRecordResponse,
)
from app.services.task_service import TaskService
from app.core.audit import log_audit_event

router = APIRouter()


@router.post(
    "/tasks", response_model=ApiResponse[TaskResponse], response_model_by_alias=True
)
def create_task(
    task_in: TaskCreate,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("task", "create")),
):
    """创建压测任务"""
    service = TaskService(db)
    try:
        task = service.create_task(task_in, user_id=actor.user_id)
    except ValueError as exc:
        detail = str(exc)
        status_code = 404 if detail == "Task not found" else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    log_audit_event(
        action="task.create",
        principal=actor,
        resource_type="task",
        resource_id=task.id,
        extra={"name": task.name},
        db=db,
    )
    return ApiResponse.success(task)


@router.get(
    "/tasks",
    response_model=ApiResponse[PageResult[TaskResponse]],
    response_model_by_alias=True,
)
def list_tasks(
    status: Optional[TaskStatus] = Query(None),
    last_run_status: Optional[RunStatus] = Query(None),
    task_id: Optional[int] = Query(None, ge=1),
    name: Optional[str] = Query(None, min_length=1),
    protocol: Optional[str] = Query(None, min_length=1),
    app_name: Optional[str] = Query(None, min_length=1),
    engine_type: Optional[str] = Query(None, min_length=1),
    env: Optional[str] = Query(None),
    script_id: Optional[int] = Query(None, ge=1),
    task_pattern: Optional[str] = Query(None, min_length=1),
    created_by: Optional[int] = Query(None, ge=1),
    created_by_name: Optional[str] = Query(None, min_length=1),
    participant_id: Optional[int] = Query(None, ge=1),
    created_from: Optional[datetime] = Query(None, alias="from"),
    created_to: Optional[datetime] = Query(None, alias="to"),
    business_line: Optional[str] = Query(None, min_length=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100, alias="pageSize"),
    db: Session = Depends(get_db),
):
    service = TaskService(db)
    tasks, total = service.list_tasks(
        status=status,
        last_run_status=last_run_status,
        task_id=task_id,
        name=name,
        protocol=protocol,
        app_name=app_name,
        engine_type=engine_type,
        env=env,
        script_id=script_id,
        task_pattern=task_pattern,
        created_by=created_by,
        created_by_name=created_by_name,
        participant_id=participant_id,
        created_from=created_from,
        created_to=created_to,
        business_line=business_line,
        page=page,
        page_size=page_size,
    )
    return ApiResponse.success(
        PageResult(items=tasks, total=total, page=page, page_size=page_size)
    )


@router.get(
    "/tasks/summary",
    response_model=ApiResponse[TaskSummaryResponse],
    response_model_by_alias=True,
)
def get_task_summary(
    env: Optional[str] = Query(None),
    created_by: Optional[int] = Query(None, ge=1),
    participant_id: Optional[int] = Query(None, ge=1),
    db: Session = Depends(get_db),
):
    service = TaskService(db)
    summary = service.get_task_summary(
        env=env,
        created_by=created_by,
        participant_id=participant_id,
    )
    return ApiResponse.success(summary)


@router.get(
    "/tasks/{task_id}",
    response_model=ApiResponse[TaskResponse],
    response_model_by_alias=True,
)
def get_task(task_id: int, db: Session = Depends(get_db)):
    service = TaskService(db)
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return ApiResponse.success(task)


@router.post(
    "/tasks/batch-stop",
    response_model=ApiResponse[TaskBatchStopResponse],
    response_model_by_alias=True,
)
def batch_stop_tasks(
    payload: TaskBatchStopRequest,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("task", "edit")),
):
    service = TaskService(db)
    try:
        response = service.batch_stop_tasks(
            payload.task_ids,
            reason=payload.reason,
            user_id=actor.user_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return ApiResponse.success(response)


@router.get(
    "/tasks/{task_id}/last-run-params",
    response_model=ApiResponse[TaskLastRunParamsResponse],
    response_model_by_alias=True,
)
def get_task_last_run_params(
    task_id: int,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("run", "create")),
):
    service = TaskService(db)
    try:
        payload = service.get_last_run_params(task_id, user_id=actor.user_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not payload:
        raise HTTPException(status_code=404, detail="Task not found")
    return ApiResponse.success(payload)


@router.get(
    "/tasks/{task_id}/versions",
    response_model=ApiResponse[PageResult[TaskVersionRecordResponse]],
    response_model_by_alias=True,
)
def list_task_versions(
    task_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100, alias="pageSize"),
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("task", "view")),
):
    service = TaskService(db)
    try:
        items, total = service.list_task_versions(
            task_id=task_id,
            page=page,
            page_size=page_size,
            user_id=actor.user_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ApiResponse.success(
        PageResult(items=items, total=total, page=page, page_size=page_size)
    )


@router.get(
    "/tasks/{task_id}/versions/{version}",
    response_model=ApiResponse[TaskVersionDetailResponse],
    response_model_by_alias=True,
)
def get_task_version_detail(
    task_id: int,
    version: str,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("task", "view")),
):
    service = TaskService(db)
    try:
        payload = service.get_task_version_detail(
            task_id=task_id,
            version=version,
            user_id=actor.user_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ApiResponse.success(payload)


@router.get(
    "/tasks/{task_id}/script-compare",
    response_model=ApiResponse[TaskScriptCompareResponse],
    response_model_by_alias=True,
)
def compare_task_script(
    task_id: int,
    history_version: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("task", "view")),
):
    service = TaskService(db)
    try:
        payload = service.compare_task_script(
            task_id=task_id,
            history_version=history_version,
            user_id=actor.user_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ApiResponse.success(payload)


@router.put(
    "/tasks/{task_id}",
    response_model=ApiResponse[TaskResponse],
    response_model_by_alias=True,
)
def update_task(
    task_id: int,
    task_in: TaskUpdate,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("task", "edit")),
):
    service = TaskService(db)
    try:
        task = service.update_task(task_id, task_in, user_id=actor.user_id)
    except PermissionError as exc:
        log_audit_event(
            action="task.update",
            principal=actor,
            resource_type="task",
            resource_id=task_id,
            outcome="forbidden",
            detail=str(exc),
            db=db,
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        detail = str(exc)
        status_code = 404 if detail == "Task not found" else 400
        log_audit_event(
            action="task.update",
            principal=actor,
            resource_type="task",
            resource_id=task_id,
            outcome="failed",
            detail=detail,
            db=db,
        )
        raise HTTPException(status_code=status_code, detail=detail) from exc
    if not task:
        log_audit_event(
            action="task.update",
            principal=actor,
            resource_type="task",
            resource_id=task_id,
            outcome="failed",
            detail="task_not_found",
            db=db,
        )
        raise HTTPException(status_code=404, detail="Task not found")
    log_audit_event(
        action="task.update",
        principal=actor,
        resource_type="task",
        resource_id=task.id,
        db=db,
    )
    return ApiResponse.success(task)


@router.post(
    "/tasks/{task_id}/prepare-run",
    response_model=ApiResponse[TaskPrepareRunResponse],
    response_model_by_alias=True,
)
def prepare_task_for_run(
    task_id: int,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("task", "edit")),
):
    service = TaskService(db)
    try:
        payload = service.prepare_task_for_run(task_id, user_id=actor.user_id)
    except PermissionError as exc:
        log_audit_event(
            action="task.prepare_run",
            principal=actor,
            resource_type="task",
            resource_id=task_id,
            outcome="forbidden",
            detail=str(exc),
            db=db,
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        detail = str(exc)
        status_code = 404 if detail == "Task not found" else 400
        log_audit_event(
            action="task.prepare_run",
            principal=actor,
            resource_type="task",
            resource_id=task_id,
            outcome="failed",
            detail=detail,
            db=db,
        )
        raise HTTPException(status_code=status_code, detail=detail) from exc
    log_audit_event(
        action="task.prepare_run",
        principal=actor,
        resource_type="task",
        resource_id=task_id,
        extra={
            "status": payload.task.status.value,
            "next_action": payload.next_action,
        },
        db=db,
    )
    return ApiResponse.success(payload)


@router.post(
    "/tasks/{task_id}/copy",
    response_model=ApiResponse[TaskResponse],
    response_model_by_alias=True,
)
def copy_task(
    task_id: int,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("task", "create")),
):
    service = TaskService(db)
    try:
        task = service.copy_task(task_id, user_id=actor.user_id)
    except PermissionError as exc:
        log_audit_event(
            action="task.copy",
            principal=actor,
            resource_type="task",
            resource_id=task_id,
            outcome="forbidden",
            detail=str(exc),
            db=db,
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        detail = str(exc)
        status_code = 404 if detail == "Task not found" else 400
        log_audit_event(
            action="task.copy",
            principal=actor,
            resource_type="task",
            resource_id=task_id,
            outcome="failed",
            detail=detail,
            db=db,
        )
        raise HTTPException(status_code=status_code, detail=detail) from exc
    log_audit_event(
        action="task.copy",
        principal=actor,
        resource_type="task",
        resource_id=task.id,
        extra={"source_task_id": task_id},
        db=db,
    )
    return ApiResponse.success(task)


@router.delete(
    "/tasks/{task_id}", response_model=ApiResponse[None], response_model_by_alias=True
)
def delete_task(
    task_id: int,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("task", "delete")),
):
    service = TaskService(db)
    try:
        service.delete_task(task_id, user_id=actor.user_id)
    except PermissionError as exc:
        log_audit_event(
            action="task.delete",
            principal=actor,
            resource_type="task",
            resource_id=task_id,
            outcome="forbidden",
            detail=str(exc),
            db=db,
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        log_audit_event(
            action="task.delete",
            principal=actor,
            resource_type="task",
            resource_id=task_id,
            outcome="failed",
            detail=str(exc),
            db=db,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_audit_event(
        action="task.delete",
        principal=actor,
        resource_type="task",
        resource_id=task_id,
        db=db,
    )
    return ApiResponse.success(None)
