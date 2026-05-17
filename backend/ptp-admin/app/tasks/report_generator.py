"""
报告生成 Celery 任务

负责生成测试报告并存储到本地文件系统
"""

import logging
from typing import Dict, Any

from celery.result import AsyncResult

from app.core.celery_app import celery_app
from app.core.database import SessionLocal
from app.models.report import ReportStatus
from app.services.mixed_run_report_service import MixedRunReportService
from app.services.report_service import ReportService
from common.schemas.mixed_run_report import (
    MixedRunReportResponse,
    MixedRunReportTaskStatusResponse,
)

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="generate_mixed_run_report_task",
    max_retries=0,
    track_started=True,
)
def generate_mixed_run_report_task(
    self,
    mixed_run_id: int,
    selected_round: int | None = None,
    selected_collection_id: int | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    del self
    db = SessionLocal()
    try:
        result = MixedRunReportService(db).generate_report(
            mixed_run_id=mixed_run_id,
            selected_round=selected_round,
            selected_collection_id=selected_collection_id,
            user_id=user_id,
        )
        return result.model_dump(mode="json")
    finally:
        db.close()


def build_mixed_run_report_task_status(
    *,
    mixed_run_id: int,
    task_id: str,
) -> MixedRunReportTaskStatusResponse:
    task_result = AsyncResult(task_id, app=celery_app)
    state = str(task_result.state or "PENDING").strip().lower()
    completed = task_result.ready()
    result_payload = None
    error = None

    if completed:
        if task_result.successful():
            raw_result = task_result.result
            if isinstance(raw_result, MixedRunReportResponse):
                result_payload = raw_result
            elif isinstance(raw_result, dict):
                result_payload = MixedRunReportResponse.model_validate(raw_result)
        else:
            error = str(task_result.result)

    return MixedRunReportTaskStatusResponse(
        mixed_run_id=mixed_run_id,
        async_task_id=task_id,
        job_status=state,
        completed=completed,
        result=result_payload,
        error=error,
    )


@celery_app.task(bind=True, name="generate_report")
def generate_report_task(self, report_id: int, report_data: Dict[str, Any]):
    """
    异步生成报告

    Args:
        report_id: 报告ID
        report_data: 报告数据
    """
    db = None
    try:
        db = SessionLocal()
        service = ReportService(db)

        logger.info(f"Starting report generation for report {report_id}")

        # 生成报告文件
        file_path = service.generate_report_file(report_id, report_data)

        logger.info(f"Report {report_id} generated successfully at {file_path}")

        return {"status": "success", "report_id": report_id, "file_path": file_path}

    except ValueError as e:
        error_msg = str(e)
        logger.error(f"Report {report_id} not found: {error_msg}")
        if db:
            service = ReportService(db)
            service.mark_report_failed(report_id, error_msg)
        raise self.retry(countdown=60, max_retries=3)

    except Exception as e:
        error_msg = f"Failed to generate report: {str(e)}"
        logger.error(f"Report {report_id} generation failed: {error_msg}")
        if db:
            service = ReportService(db)
            service.mark_report_failed(report_id, error_msg)
        raise self.retry(countdown=120, max_retries=3)

    finally:
        if db:
            db.close()


def build_run_report_task_status(
    *,
    run_id: int,
    report_id: int,
    task_id: str,
) -> dict[str, Any]:
    task_result = AsyncResult(task_id, app=celery_app)
    state = str(task_result.state or "PENDING").strip().lower()
    completed = task_result.ready()
    result_payload = None
    error = None

    db = SessionLocal()
    try:
        service = ReportService(db)
        report = service.get_report(report_id)
        if report and report.status == ReportStatus.COMPLETED:
            completed = True
            state = "success" if state in {"pending", "started"} else state
            result_payload = {
                "status": "ready",
                "message": f"报告 #{report_id} 已生成",
                "report_id": int(report.id),
                "report_name": report.name,
                "generated": True,
            }
        elif report and report.status == ReportStatus.FAILED:
            completed = True
            state = "failure" if state in {"pending", "started"} else state
            metrics_data = (
                report.metrics_data if isinstance(report.metrics_data, dict) else {}
            )
            error = str(metrics_data.get("error") or "报告生成失败")
    finally:
        db.close()

    if completed and result_payload is None:
        if task_result.successful():
            raw_result = task_result.result
            if isinstance(raw_result, dict):
                result_payload = {
                    "status": "ready",
                    "message": f"报告 #{report_id} 已生成",
                    "report_id": int(raw_result.get("report_id") or report_id),
                    "generated": True,
                }
        elif error is None:
            error = str(task_result.result)

    return {
        "run_id": run_id,
        "report_id": report_id,
        "async_task_id": task_id,
        "job_status": state,
        "completed": completed,
        "result": result_payload,
        "error": error,
    }


@celery_app.task(name="generate_test_summary_report")
def generate_test_summary_report(task_id: int, test_results: Dict[str, Any]):
    """
    生成测试总结报告

    Args:
        task_id: 任务ID
        test_results: 测试结果数据
    """
    db = None
    try:
        db = SessionLocal()
        from app.services.task_service import TaskService
        from app.schemas.report import ReportCreate
        from app.models.report import ReportType

        task_service = TaskService(db)

        # 获取任务信息
        task = task_service.get_task(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")

        # 创建报告记录
        service = ReportService(db)
        report_in = ReportCreate(
            name=f"测试报告 - {task.name}",
            description=f"任务 {task.name} 的性能测试报告",
            report_type=(
                ReportType.JMETER
                if str(task.engine_type).lower().endswith("jmeter")
                else ReportType.K6
            ),
            task_id=task_id,
            run_id=test_results.get("run_id"),
            test_config=getattr(task, "properties", None),
        )

        report = service.create_report(report_in, user_id=task.created_by)

        # 异步生成报告
        generate_report_task.delay(report.id, test_results)

        logger.info(
            f"Report generation task created for task {task_id}, report ID: {report.id}"
        )

        return {"status": "success", "report_id": report.id, "task_id": task_id}

    except Exception as e:
        error_msg = f"Failed to create test summary report: {str(e)}"
        logger.error(error_msg)
        raise

    finally:
        if db:
            db.close()
