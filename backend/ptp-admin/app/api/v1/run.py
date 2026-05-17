from __future__ import annotations

import importlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import (
    ActorPrincipal,
    ensure_write_role_or_raise,
    get_actor_principal,
    get_db,
)
from app.core.permissions import require_permission
from app.models.run import Run
from app.schemas.response import ApiResponse, PageResult
from app.schemas.run import (
    RunBaselineResponse,
    RunBaselineSetRequest,
    RunVerdictResponse,
    LogsResponse,
    MetricsResponse,
    RunChecksResponse,
    RunCreate,
    RunDashboardsResponse,
    RunCompareResponse,
    RunPodMonitorResponse,
    RunPodStatusResponse,
    RunProcessResponse,
    RunResponse,
    RunStopRequest,
    RunSummaryMetricsResponse,
    EndpointTrendResponse,
)
from app.services.run_service import RunService
from common.config.settings import settings
from common.models.enums import RunBaselineScopeType, RunStatus
from app.core.audit import log_audit_event

router = APIRouter()
_SERVICE_ROOT = Path(__file__).resolve().parents[3]
_BACKEND_ROOT = _SERVICE_ROOT.parent
_EXECUTE_TEST_TASK_MODULE = "app.tasks.test_executor"


def _is_dispatch_task_import_error(exc: ModuleNotFoundError) -> bool:
    missing_name = str(getattr(exc, "name", "") or "").strip()
    if missing_name in {"app", "app.tasks", _EXECUTE_TEST_TASK_MODULE}:
        return True
    return missing_name.startswith("app.tasks.")


def _bootstrap_dispatch_import_path() -> None:
    for candidate in (_BACKEND_ROOT, _SERVICE_ROOT):
        candidate_str = str(candidate)
        if candidate_str not in sys.path:
            sys.path.insert(0, candidate_str)


def _dispatch_import_failure(exc: ModuleNotFoundError) -> RuntimeError:
    return RuntimeError(
        "dispatch_import_failed: cannot import app.tasks.test_executor; "
        f"cwd={Path.cwd()}; "
        f"service_root={_SERVICE_ROOT} exists={_SERVICE_ROOT.exists()}; "
        f"backend_root={_BACKEND_ROOT} exists={_BACKEND_ROOT.exists()}; "
        "hint=host ptp-admin/ptp-worker import path is stale; please restart the service from the current repository root"
    )


def _load_execute_test_task():
    try:
        return importlib.import_module(_EXECUTE_TEST_TASK_MODULE).execute_test_task
    except ModuleNotFoundError as exc:
        if not _is_dispatch_task_import_error(exc):
            raise

    _bootstrap_dispatch_import_path()
    try:
        return importlib.import_module(_EXECUTE_TEST_TASK_MODULE).execute_test_task
    except ModuleNotFoundError as exc:
        if not _is_dispatch_task_import_error(exc):
            raise
        raise _dispatch_import_failure(exc) from exc


def _ensure_run_exists_lightweight(service: RunService, run_id: int) -> None:
    if service.repo.find_by_id(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")


def _mark_enqueue_failed(run_id: int, detail: str) -> None:
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        run = db.query(Run).filter(Run.run_id == run_id).first()
        if not run or run.run_status != RunStatus.PREPARING:
            return
        ended_at = datetime.now(timezone.utc)
        started_at = run.started_at
        if started_at and started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        run.run_status = RunStatus.FAILED
        run.run_status_detail = "dispatch_failed"
        run.stop_reason = detail
        run.ended_at = ended_at
        if started_at:
            run.duration_seconds = max(0, int((ended_at - started_at).total_seconds()))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _dispatch_run_to_worker(run_id: int) -> None:
    import logging

    logger = logging.getLogger(__name__)
    try:
        execute_test_task = _load_execute_test_task()
        logger.info("Dispatching Celery task for run %s", run_id)
        task_result = execute_test_task.delay(run_id)
        logger.info(
            "Celery task dispatched for run %s: task_id=%s",
            run_id,
            getattr(task_result, "id", None),
        )
    except RuntimeError as exc:
        logger.exception("Failed to dispatch Celery task for run %s", run_id)
        if (
            "Retry limit exceeded while trying to reconnect to the Celery redis result store backend"
            in str(exc)
        ):
            logger.warning(
                "Celery redis backend unreachable for run %s; leaving run in PREPARING",
                run_id,
            )
            return
        _mark_enqueue_failed(run_id, f"dispatch_enqueue_failed: {exc}")
    except Exception as exc:
        logger.exception("Failed to dispatch Celery task for run %s", run_id)
        _mark_enqueue_failed(run_id, f"dispatch_enqueue_failed: {exc}")


@router.get(
    "/runs",
    response_model=ApiResponse[PageResult[RunResponse]],
    response_model_by_alias=True,
)
def list_runs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100, alias="pageSize"),
    task_id: Optional[int] = Query(None, ge=1),
    task_name: Optional[str] = Query(None),
    run_id: Optional[int] = Query(None, ge=1),
    run_status: Optional[RunStatus] = Query(None),
    engine_type: Optional[str] = Query(None),
    protocol: Optional[str] = Query(None),
    business_line: Optional[str] = Query(None),
    task_pattern: Optional[str] = Query(None),
    operator_name: Optional[str] = Query(None),
    env: Optional[str] = Query(None),
    started_from: Optional[datetime] = Query(None, alias="from"),
    started_to: Optional[datetime] = Query(None, alias="to"),
    db: Session = Depends(get_db),
):
    service = RunService(db)
    runs, total = service.list_runs(
        page=page,
        page_size=page_size,
        task_id=task_id,
        task_name=task_name,
        run_id=run_id,
        run_status=run_status,
        engine_type=engine_type,
        protocol=protocol,
        business_line=business_line,
        task_pattern=task_pattern,
        env=env,
        operator_name=operator_name,
        started_from=started_from,
        started_to=started_to,
    )
    return ApiResponse.success(
        PageResult(items=runs, total=total, page=page, page_size=page_size)
    )


@router.post(
    "/runs", response_model=ApiResponse[RunResponse], response_model_by_alias=True
)
def create_run(
    run_in: RunCreate,
    background_tasks: BackgroundTasks,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("run", "create")),
):
    import logging

    logger = logging.getLogger(__name__)
    service = RunService(db)
    try:
        run = service.create_run(
            run_in, user_id=actor.user_id, idempotency_key=idempotency_key
        )
        testing_env = os.environ.get("TESTING", "0")
        is_testing = testing_env == "1"
        logger.info(
            f"Created run {run.run_id}, TESTING={testing_env}, is_testing={is_testing}"
        )

        if not is_testing:
            background_tasks.add_task(_dispatch_run_to_worker, run.run_id)
        else:
            execute_test_task = _load_execute_test_task()

            logger.warning(
                f"Executing run {run.run_id} synchronously (TESTING={testing_env})"
            )
            execute_test_task(run.run_id)
        log_audit_event(
            action="run.create",
            principal=actor,
            resource_type="run",
            resource_id=run.run_id,
            extra={"task_id": run.task_id},
            db=db,
        )
        return ApiResponse.success(run)
    except ValueError as exc:
        log_audit_event(
            action="run.create",
            principal=actor,
            resource_type="run",
            outcome="failed",
            detail=str(exc),
            extra={"task_id": run_in.task_id},
            db=db,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        log_audit_event(
            action="run.create",
            principal=actor,
            resource_type="run",
            outcome="forbidden",
            detail=str(exc),
            extra={"task_id": run_in.task_id},
            db=db,
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get(
    "/runs/compare",
    response_model=ApiResponse[RunCompareResponse],
    response_model_by_alias=True,
)
def get_run_compare(
    base_id: int = Query(..., ge=1),
    comparator_id: int = Query(..., ge=1),
    db: Session = Depends(get_db),
):
    service = RunService(db)
    try:
        payload = service.get_run_compare(base_id=base_id, comparator_id=comparator_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ApiResponse.success(payload)


@router.get(
    "/runs/{run_id}/baseline",
    response_model=ApiResponse[Optional[RunBaselineResponse]],
    response_model_by_alias=True,
)
def get_run_baseline(
    run_id: int,
    scope_type: Optional[RunBaselineScopeType] = Query(None),
    db: Session = Depends(get_db),
):
    service = RunService(db)
    run = service.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return ApiResponse.success(service.get_run_baseline(run_id, scope_type=scope_type))


@router.post(
    "/runs/{run_id}/baseline",
    response_model=ApiResponse[RunBaselineResponse],
    response_model_by_alias=True,
)
def set_run_baseline(
    run_id: int,
    body: RunBaselineSetRequest,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("run", "create")),
):
    service = RunService(db)
    try:
        baseline = service.set_run_baseline(run_id, body)
    except ValueError as exc:
        status_code = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    log_audit_event(
        action="run.set_baseline",
        principal=actor,
        resource_type="run_baseline",
        resource_id=baseline.baseline_id,
        extra={
            "run_id": run_id,
            "baseline_run_id": baseline.baseline_run_id,
            "scope_type": baseline.scope_type.value,
            "scope_key": baseline.scope_key,
        },
        db=db,
    )
    return ApiResponse.success(baseline)


@router.get(
    "/runs/{run_id}/verdict",
    response_model=ApiResponse[RunVerdictResponse],
    response_model_by_alias=True,
)
def get_run_verdict(run_id: int, db: Session = Depends(get_db)):
    service = RunService(db)
    verdict = service.get_run_verdict(run_id)
    if verdict is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return ApiResponse.success(verdict)



@router.get(
    "/runs/{run_id}",
    response_model=ApiResponse[RunResponse],
    response_model_by_alias=True,
)
def get_run(run_id: int, db: Session = Depends(get_db)):
    service = RunService(db)
    run = service.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return ApiResponse.success(run)


@router.post(
    "/runs/{run_id}/stop",
    response_model=ApiResponse[RunResponse],
    response_model_by_alias=True,
)
def stop_run(
    run_id: int,
    body: RunStopRequest = RunStopRequest(),
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(require_permission("run", "stop")),
):
    service = RunService(db)
    try:
        run = service.stop_run(run_id, reason=body.reason, user_id=actor.user_id)
    except PermissionError as exc:
        log_audit_event(
            action="run.stop",
            principal=actor,
            resource_type="run",
            resource_id=run_id,
            outcome="forbidden",
            detail=str(exc),
            db=db,
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not run:
        log_audit_event(
            action="run.stop",
            principal=actor,
            resource_type="run",
            resource_id=run_id,
            outcome="failed",
            detail="run_not_found",
            db=db,
        )
        raise HTTPException(status_code=404, detail="Run not found")
    log_audit_event(
        action="run.stop",
        principal=actor,
        resource_type="run",
        resource_id=run.run_id,
        extra={"reason": body.reason},
        db=db,
    )
    return ApiResponse.success(run)




@router.get(
    "/runs/{run_id}/metrics",
    response_model=ApiResponse[MetricsResponse],
    response_model_by_alias=True,
)
def get_run_metrics(
    run_id: int,
    started_from: Optional[datetime] = Query(None, alias="from"),
    started_to: Optional[datetime] = Query(None, alias="to"),
    metric: Optional[str] = Query(None),
    step_seconds: int = Query(10, ge=1, le=3600),
    db: Session = Depends(get_db),
):
    service = RunService(db)
    _ensure_run_exists_lightweight(service, run_id)
    metrics = service.get_metrics(
        run_id=run_id,
        started_from=started_from,
        started_to=started_to,
        metric=metric,
        step_seconds=step_seconds,
    )
    return ApiResponse.success(metrics)


@router.get(
    "/runs/{run_id}/logs",
    response_model=ApiResponse[LogsResponse],
    response_model_by_alias=True,
)
def get_run_logs(
    run_id: int,
    cursor: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=2000),
    view: str = Query("all", pattern="^(all|exception)$"),
    level: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    db: Session = Depends(get_db),
):
    service = RunService(db)
    _ensure_run_exists_lightweight(service, run_id)
    logs = service.get_logs(
        run_id=run_id,
        cursor=cursor,
        limit=limit,
        view=view,
        level=level,
        source=source,
        order=order,
    )
    return ApiResponse.success(logs)


@router.get(
    "/runs/{run_id}/summary-metrics",
    response_model=ApiResponse[RunSummaryMetricsResponse],
    response_model_by_alias=True,
)
def get_run_summary_metrics(
    run_id: int,
    db: Session = Depends(get_db),
):
    """获取接口级核心指标表（用于结果详情页核心性能指标表格）。"""
    service = RunService(db)
    _ensure_run_exists_lightweight(service, run_id)
    summary = service.get_summary_metrics(run_id)
    return ApiResponse.success(summary)


@router.get(
    "/runs/{run_id}/checks",
    response_model=ApiResponse[RunChecksResponse],
    response_model_by_alias=True,
)
def get_run_checks(
    run_id: int,
    db: Session = Depends(get_db),
):
    """获取 Group-Checks 表（K6 场景强依赖，JMeter/Agent 场景允许返回空数组）。"""
    service = RunService(db)
    _ensure_run_exists_lightweight(service, run_id)
    checks = service.get_checks(run_id)
    return ApiResponse.success(checks)


@router.get(
    "/runs/{run_id}/pods",
    response_model=ApiResponse[RunPodStatusResponse],
    response_model_by_alias=True,
)
def get_run_pods(
    run_id: int,
    db: Session = Depends(get_db),
):
    """获取执行节点/Pod 状态列表。"""
    service = RunService(db)
    _ensure_run_exists_lightweight(service, run_id)
    pods = service.get_pods(run_id)
    return ApiResponse.success(pods)


@router.get(
    "/runs/{run_id}/pods/monitor",
    response_model=ApiResponse[RunPodMonitorResponse],
    response_model_by_alias=True,
)
def get_run_pods_monitor(
    run_id: int,
    step_seconds: int = Query(10, ge=1, le=3600),
    db: Session = Depends(get_db),
):
    """获取执行节点资源监控数据。"""
    service = RunService(db)
    _ensure_run_exists_lightweight(service, run_id)
    monitor = service.get_pods_monitor(run_id, step_seconds=step_seconds)
    return ApiResponse.success(monitor)


@router.get(
    "/runs/{run_id}/dashboards",
    response_model=ApiResponse[RunDashboardsResponse],
    response_model_by_alias=True,
)
def get_run_dashboards(
    run_id: int,
    db: Session = Depends(get_db),
):
    """获取关联监控看板入口列表（引擎 Grafana、Pod Grafana、关联监控等）。"""
    service = RunService(db)
    _ensure_run_exists_lightweight(service, run_id)
    dashboards = service.get_dashboards(run_id)
    return ApiResponse.success(dashboards)


@router.get(
    "/runs/{run_id}/process",
    response_model=ApiResponse[RunProcessResponse],
    response_model_by_alias=True,
)
def get_run_process(
    run_id: int,
    db: Session = Depends(get_db),
):
    """获取运行过程信息（用于右上角运行时控制或详情顶部状态流转展示）。"""
    service = RunService(db)
    _ensure_run_exists_lightweight(service, run_id)
    process = service.get_process(run_id)
    return ApiResponse.success(process)


@router.get(
    "/runs/{run_id}/endpoint-trends",
    response_model=ApiResponse[EndpointTrendResponse],
    response_model_by_alias=True,
)
def get_run_endpoint_trends(
    run_id: int,
    metric: Optional[str] = Query(
        None,
        description="Filter by metric name: rt_avg_ms, rt_p95_ms, rt_p99_ms, throughput, error_rate",
    ),
    endpoint_name: Optional[str] = Query(None, description="Filter by endpoint name"),
    step_seconds: int = Query(10, ge=1, le=3600),
    db: Session = Depends(get_db),
):
    """获取接口级趋势数据（用于多接口多折线图）。

    返回各接口的时序数据，支持按指标和接口名过滤。
    前端可通过此接口实现接口维度趋势图。
    """
    service = RunService(db)
    _ensure_run_exists_lightweight(service, run_id)
    trends = service.get_endpoint_trends(
        run_id=run_id,
        metric=metric,
        endpoint_name=endpoint_name,
        step_seconds=step_seconds,
    )
    return ApiResponse.success(trends)
