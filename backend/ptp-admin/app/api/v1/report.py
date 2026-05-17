"""
报告管理 API 路由
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.deps import (
    ActorPrincipal,
    ensure_write_role_or_raise,
    get_actor_principal,
    get_db,
)
from app.schemas.report import (
    ReportCreate,
    ReportUpdate,
    ReportResponse,
    ReportStatistics,
)
from app.schemas.response import ApiResponse, PageResult
from app.services.report_service import ReportService, RunReportUnavailableError
from app.models.report import ReportStatus, ReportType

router = APIRouter()


@router.post(
    "/reports", response_model=ApiResponse[ReportResponse], response_model_by_alias=True
)
def create_report(
    report_in: ReportCreate,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(get_actor_principal),
):
    """创建报告"""
    ensure_write_role_or_raise(actor, db=db)
    service = ReportService(db)
    report = service.create_report(report_in, user_id=actor.user_id)
    return ApiResponse.success(report)


@router.get(
    "/reports",
    response_model=ApiResponse[PageResult[ReportResponse]],
    response_model_by_alias=True,
)
def list_reports(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=1000, alias="pageSize"),
    task_id: Optional[int] = Query(None, description="任务ID筛选"),
    run_id: Optional[int] = Query(None, description="执行记录ID筛选"),
    status: Optional[ReportStatus] = Query(None, description="状态筛选"),
    report_type: Optional[ReportType] = Query(None, description="类型筛选"),
    db: Session = Depends(get_db),
):
    """获取报告列表"""
    service = ReportService(db)
    skip = (page - 1) * page_size
    reports, total = service.list_reports(
        skip=skip,
        limit=page_size,
        task_id=task_id,
        run_id=run_id,
        status=status,
        report_type=report_type,
    )
    return ApiResponse.success(
        PageResult(items=reports, total=total, page=page, page_size=page_size)
    )


@router.get(
    "/reports/search",
    response_model=ApiResponse[PageResult[ReportResponse]],
    response_model_by_alias=True,
)
def search_reports(
    keyword: str = Query(..., description="搜索关键词"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000, alias="pageSize"),
    db: Session = Depends(get_db),
):
    """搜索报告"""
    service = ReportService(db)
    skip = (page - 1) * page_size
    reports = service.search_reports(keyword, skip, page_size)
    total = len(reports)
    return ApiResponse.success(
        PageResult(items=reports, total=total, page=page, page_size=page_size)
    )


@router.get(
    "/reports/statistics",
    response_model=ApiResponse[ReportStatistics],
    response_model_by_alias=True,
)
def get_report_statistics(db: Session = Depends(get_db)):
    """获取报告统计信息"""
    service = ReportService(db)
    return ApiResponse.success(service.get_statistics())


@router.get(
    "/reports/{report_id}",
    response_model=ApiResponse[ReportResponse],
    response_model_by_alias=True,
)
def get_report(report_id: int, db: Session = Depends(get_db)):
    """获取报告详情"""
    service = ReportService(db)
    report = service.get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return ApiResponse.success(report)


@router.get("/reports/{report_id}/download")
def download_report(report_id: int, db: Session = Depends(get_db)):
    """下载报告文件"""
    service = ReportService(db)
    try:
        path = service.get_report_file_path(report_id, regenerate_missing=True)
        return FileResponse(path, filename=path.name, media_type="text/html")
    except RunReportUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Report file not found")


@router.get(
    "/reports/frontdoor/run/{run_id}",
    response_model=ApiResponse[dict],
    response_model_by_alias=True,
)
def resolve_run_report_frontdoor(run_id: int, db: Session = Depends(get_db)):
    """解析 Run 下载报告前门，只返回当前模板兼容的目标。"""
    service = ReportService(db)
    try:
        return ApiResponse.success(service.resolve_download_frontdoor(run_id))
    except RunReportUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/reports/frontdoor/run/{run_id}/ensure",
    response_model=ApiResponse[dict],
    response_model_by_alias=True,
)
def ensure_run_report_frontdoor(
    run_id: int,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(get_actor_principal),
):
    """缺失或待刷新模板时生成当前模板报告，并返回可下载前门。"""
    ensure_write_role_or_raise(actor, db=db)
    service = ReportService(db)
    try:
        return ApiResponse.success(
            service.ensure_download_frontdoor(run_id, user_id=actor.user_id)
        )
    except RunReportUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/reports/frontdoor/run/{run_id}/ensure/async",
    response_model=ApiResponse[dict],
    response_model_by_alias=True,
)
def ensure_run_report_frontdoor_async(
    run_id: int,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(get_actor_principal),
):
    """缺失或待刷新模板时异步生成当前模板报告，并返回后台任务。"""
    ensure_write_role_or_raise(actor, db=db)
    service = ReportService(db)
    try:
        return ApiResponse.success(
            service.ensure_download_frontdoor_async(run_id, user_id=actor.user_id)
        )
    except RunReportUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/reports/frontdoor/run/{run_id}/regenerate",
    response_model=ApiResponse[dict],
    response_model_by_alias=True,
)
def regenerate_run_report_frontdoor(
    run_id: int,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(get_actor_principal),
):
    """强制重新生成当前模板 Run 报告，并返回可查看/下载前门。"""
    ensure_write_role_or_raise(actor, db=db)
    service = ReportService(db)
    try:
        return ApiResponse.success(
            service.regenerate_download_frontdoor(run_id, user_id=actor.user_id)
        )
    except RunReportUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/reports/frontdoor/run/{run_id}/regenerate/async",
    response_model=ApiResponse[dict],
    response_model_by_alias=True,
)
def regenerate_run_report_frontdoor_async(
    run_id: int,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(get_actor_principal),
):
    """强制异步重新生成当前模板 Run 报告。"""
    ensure_write_role_or_raise(actor, db=db)
    service = ReportService(db)
    try:
        return ApiResponse.success(
            service.regenerate_download_frontdoor_async(run_id, user_id=actor.user_id)
        )
    except RunReportUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/reports/frontdoor/run/{run_id}/tasks/{task_id}",
    response_model=ApiResponse[dict],
    response_model_by_alias=True,
)
def get_run_report_frontdoor_task_status(
    run_id: int,
    task_id: str,
    report_id: int = Query(..., ge=1),
):
    """查询普通 Run 报告后台生成任务状态。"""
    from app.tasks.report_generator import build_run_report_task_status

    return ApiResponse.success(
        build_run_report_task_status(
            run_id=run_id,
            report_id=report_id,
            task_id=task_id,
        )
    )


@router.put(
    "/reports/{report_id}",
    response_model=ApiResponse[ReportResponse],
    response_model_by_alias=True,
)
def update_report(
    report_id: int,
    report_in: ReportUpdate,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(get_actor_principal),
):
    """更新报告"""
    ensure_write_role_or_raise(actor, db=db)
    service = ReportService(db)
    report = service.update_report(report_id, report_in)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return ApiResponse.success(report)


@router.delete(
    "/reports/{report_id}",
    response_model=ApiResponse[None],
    response_model_by_alias=True,
)
def delete_report(
    report_id: int,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(get_actor_principal),
):
    """删除报告"""
    ensure_write_role_or_raise(actor, db=db)
    service = ReportService(db)
    success = service.delete_report(report_id)
    if not success:
        raise HTTPException(status_code=404, detail="Report not found")
    return ApiResponse.success(None)


@router.get(
    "/reports/task/{task_id}",
    response_model=ApiResponse[List[ReportResponse]],
    response_model_by_alias=True,
)
def get_reports_by_task(task_id: int, db: Session = Depends(get_db)):
    """获取任务的所有报告"""
    service = ReportService(db)
    reports = service.get_reports_by_task(task_id)
    return ApiResponse.success(reports)


@router.post(
    "/reports/{report_id}/generate",
    response_model=ApiResponse[dict],
    response_model_by_alias=True,
)
def generate_report(
    report_id: int,
    report_data: dict,
    db: Session = Depends(get_db),
    actor: ActorPrincipal = Depends(get_actor_principal),
):
    """生成报告文件"""
    ensure_write_role_or_raise(actor, db=db)
    service = ReportService(db)
    try:
        file_path = service.generate_report_file(report_id, report_data)
        return ApiResponse.success(
            {"status": "success", "report_id": report_id, "file_path": file_path}
        )
    except RunReportUnavailableError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        service.mark_report_failed(report_id, str(e))
        raise HTTPException(
            status_code=500, detail=f"Failed to generate report: {str(e)}"
        )
