"""报告业务服务层。"""

import html
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.models.report import Report, ReportStatus, ReportType
from app.models.run import Run
from app.models.task import Task
from app.schemas.report import ReportCreate, ReportUpdate, ReportStatistics
from app.services.report_observability_analysis_service import (
    ReportObservabilityAnalysisService,
)
from app.services.report_quality_gate_service import ReportQualityGateService
from app.services.run_service import RunService
from app.repositories.report_repository import ReportRepository
from common.utils import s3_utils
from common.config.settings import get_report_artifact_prefix, settings

logger = logging.getLogger(__name__)

REPORT_DISPLAY_TIMEZONE = timezone(timedelta(hours=8), "CST")
REPORT_DISPLAY_TIMEZONE_NAME = "Asia/Shanghai"
REPORT_TEMPLATE_VERSION = "run-report-v3-asia-shanghai-protocols"
REPORTS_DIR_ENV = "PTP_REPORTS_DIR"


class RunReportUnavailableError(ValueError):
    """Raised when a Run report action is attempted before the Run is terminal."""


def resolve_reports_dir() -> Path:
    configured = os.getenv(REPORTS_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()

    root_dir = Path(__file__).resolve().parent.parent.parent.parent
    return root_dir / "tmp_reports"


class ReportService:
    """报告业务服务"""

    CURRENT_TEMPLATE_MARKERS = (
        f'data-report-template-version="{REPORT_TEMPLATE_VERSION}"',
        "报告时区：Asia/Shanghai",
        "趋势曲线",
        "接口 TPS 趋势",
        "接口 Avg 响应时间趋势",
        "接口 P95 响应时间趋势",
        "接口 P99 响应时间趋势",
    )
    TEMPLATE_REFRESH_MESSAGE = "当前只有待刷新模板报告，请重新生成最新版后再下载"
    MISSING_REPORT_MESSAGE = "当前 Run 暂无可下载报告，请先生成报告"
    TERMINAL_RUN_STATUSES = {"succeeded", "failed", "stopped"}

    def __init__(self, db):
        self.repo = ReportRepository(db)
        self.reports_dir = resolve_reports_dir()
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    def create_report(
        self, report_in: ReportCreate, user_id: Optional[int] = None
    ) -> Report:
        """创建报告"""
        report_data = report_in.model_dump()
        report_data["status"] = ReportStatus.PENDING
        if user_id:
            report_data["generated_by"] = user_id

        return self.repo.create(report_data)

    def get_report(self, report_id: int) -> Optional[Report]:
        """获取报告详情"""
        report = self.repo.get_by_id(report_id)
        if report is None:
            return None
        self._attach_quality_gate(report)
        return report

    def _attach_quality_gate(self, report: Report) -> None:
        report.quality_gate = ReportQualityGateService.evaluate(
            report,
            current_template=self.is_current_template_report(report),
            has_report_file=self._has_report_file(report),
        )

    def get_reports_by_task(self, task_id: int) -> List[Report]:
        """获取任务的所有报告"""
        return self.repo.get_by_task_id(task_id)

    def get_reports_by_run(self, run_id: int) -> List[Report]:
        """获取执行记录的所有报告"""
        return self.repo.get_by_run_id(run_id)

    def _report_metrics_match_current_template(self, report: Report) -> bool:
        metrics_data = (
            report.metrics_data if isinstance(report.metrics_data, dict) else {}
        )
        if metrics_data.get("template_version") != REPORT_TEMPLATE_VERSION:
            return False
        endpoint_trends = metrics_data.get("endpoint_trends")
        if not isinstance(endpoint_trends, dict):
            return False
        return any(
            isinstance(endpoint_trends.get(metric), dict)
            for metric in ("throughput", "rt_avg_ms", "rt_p95_ms", "rt_p99_ms")
        )

    def _report_file_matches_current_template(self, report: Report) -> bool:
        try:
            path = self._resolve_report_file_path(report)
        except FileNotFoundError:
            return False

        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return False

        return all(marker in content for marker in self.CURRENT_TEMPLATE_MARKERS)

    def is_current_template_report(self, report: Report) -> bool:
        """判断报告是否兼容当前统一 HTML 模板版本。"""
        return self._report_file_matches_current_template(report)

    def _has_report_file(self, report: Report) -> bool:
        try:
            self._resolve_report_file_path(report)
        except FileNotFoundError:
            return False
        return True

    @classmethod
    def _run_status_value(cls, run: Run) -> str:
        return str(getattr(run.run_status, "value", run.run_status) or "").lower()

    @classmethod
    def _ensure_run_terminal_for_report(cls, run: Run, *, action: str) -> None:
        status = cls._run_status_value(run)
        if status not in cls.TERMINAL_RUN_STATUSES:
            raise RunReportUnavailableError(
                f"压测完成后才能{action}，当前状态：{status or 'unknown'}"
            )

    def resolve_download_frontdoor(self, run_id: int) -> Dict[str, Any]:
        """按当前模板兼容性解析 Run 下载前门。"""
        run = self.repo.db.query(Run).filter(Run.run_id == run_id).first()
        if run is not None:
            self._ensure_run_terminal_for_report(run, action="查看报告")
        ai_report_metadata = self._load_latest_ai_report_metadata(run_id)
        completed_reports = self.repo.get_completed_by_run_id(run_id)
        return self._resolve_download_frontdoor_from_reports(
            run_id=run_id,
            completed_reports=completed_reports,
            ai_report_metadata=ai_report_metadata,
        )

    def resolve_download_frontdoors(
        self, run_ids: List[int]
    ) -> Dict[int, Dict[str, Any]]:
        """批量解析 Run 下载前门，避免批次详情按任务项重复查库。"""
        ordered_run_ids: List[int] = []
        seen: set[int] = set()
        for raw_run_id in run_ids:
            try:
                run_id = int(raw_run_id)
            except (TypeError, ValueError):
                continue
            if run_id <= 0 or run_id in seen:
                continue
            seen.add(run_id)
            ordered_run_ids.append(run_id)
        if not ordered_run_ids:
            return {}

        ai_metadata_by_run = self._load_latest_ai_report_metadata_by_run_ids(
            ordered_run_ids
        )
        completed_reports_by_run = self._load_completed_reports_by_run_ids(
            ordered_run_ids
        )
        return {
            run_id: self._resolve_download_frontdoor_from_reports(
                run_id=run_id,
                completed_reports=completed_reports_by_run.get(run_id, []),
                ai_report_metadata=ai_metadata_by_run.get(run_id),
            )
            for run_id in ordered_run_ids
        }

    def _resolve_download_frontdoor_from_reports(
        self,
        *,
        run_id: int,
        completed_reports: List[Report],
        ai_report_metadata: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """基于已加载报告解析下载前门。"""
        for report in completed_reports:
            if self.is_current_template_report(report):
                return {
                    "status": "ready",
                    "message": f"已选择兼容当前模板的报告 #{report.id}",
                    "report_id": report.id,
                    "report_name": report.name,
                    "ai_report": ai_report_metadata,
                }

        available_reports = [
            report for report in completed_reports if self._has_report_file(report)
        ]
        if available_reports:
            latest_report = available_reports[0]
            return {
                "status": "template_fallback",
                "message": self.TEMPLATE_REFRESH_MESSAGE,
                "report_id": latest_report.id,
                "report_name": latest_report.name,
                "ai_report": ai_report_metadata,
            }

        return {
            "status": "missing",
            "message": self.MISSING_REPORT_MESSAGE,
            "report_id": None,
            "report_name": None,
            "ai_report": ai_report_metadata,
        }

    def ensure_download_frontdoor(
        self, run_id: int, *, user_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """确保 Run 有当前模板报告可下载；缺失或待刷新模板时同步生成最新版。"""
        run = self.repo.db.query(Run).filter(Run.run_id == run_id).first()
        if run is None:
            raise ValueError(f"Run {run_id} not found")
        self._ensure_run_terminal_for_report(run, action="生成或查看报告")

        current = self.resolve_download_frontdoor(run_id)
        if current.get("status") == "ready" and current.get("report_id"):
            current["generated"] = False
            return current

        report = self._create_current_template_report(run, user_id=user_id)
        refreshed = self.resolve_download_frontdoor(run_id)
        refreshed["generated"] = True
        refreshed["report_id"] = int(report.id)
        refreshed["report_name"] = report.name
        refreshed["message"] = f"已重新生成当前模板报告 #{report.id}"
        return refreshed

    def ensure_download_frontdoor_async(
        self, run_id: int, *, user_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """确保 Run 有当前模板报告可下载；缺失或待刷新模板时提交后台生成任务。"""
        run = self.repo.db.query(Run).filter(Run.run_id == run_id).first()
        if run is None:
            raise ValueError(f"Run {run_id} not found")
        self._ensure_run_terminal_for_report(run, action="生成或查看报告")

        current = self.resolve_download_frontdoor(run_id)
        if current.get("status") == "ready" and current.get("report_id"):
            current.update(
                {
                    "accepted": False,
                    "completed": True,
                    "generated": False,
                    "async_task_id": None,
                }
            )
            return current
        return self._submit_current_template_report_task(
            run,
            user_id=user_id,
            message_prefix="已提交当前模板报告生成任务",
        )

    def regenerate_download_frontdoor(
        self, run_id: int, *, user_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """强制为 Run 重新生成当前模板报告，并返回最新可查看/下载前门。"""
        run = self.repo.db.query(Run).filter(Run.run_id == run_id).first()
        if run is None:
            raise ValueError(f"Run {run_id} not found")
        self._ensure_run_terminal_for_report(run, action="重新生成报告")

        report = self._create_current_template_report(run, user_id=user_id)
        return {
            "status": "ready",
            "message": f"已重新生成当前模板报告 #{report.id}",
            "report_id": int(report.id),
            "report_name": report.name,
            "generated": True,
            "ai_report": self._load_latest_ai_report_metadata(run_id),
        }

    def regenerate_download_frontdoor_async(
        self, run_id: int, *, user_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """强制为 Run 提交当前模板报告后台生成任务。"""
        run = self.repo.db.query(Run).filter(Run.run_id == run_id).first()
        if run is None:
            raise ValueError(f"Run {run_id} not found")
        self._ensure_run_terminal_for_report(run, action="重新生成报告")
        return self._submit_current_template_report_task(
            run,
            user_id=user_id,
            message_prefix="已提交当前模板报告重新生成任务",
        )

    def _run_report_type(self, run: Run) -> ReportType:
        return (
            ReportType.JMETER
            if str(getattr(run.engine_type, "value", run.engine_type))
            .lower()
            .endswith("jmeter")
            else ReportType.K6
        )

    @staticmethod
    def _derive_request_count(total: Any, rate: Any) -> Optional[int]:
        if not isinstance(total, (int, float)) or total < 0:
            return None
        if not isinstance(rate, (int, float)):
            return None
        normalized = float(rate) if 0 <= float(rate) <= 1 else float(rate) / 100.0
        if normalized < 0:
            return None
        return int(round(float(total) * min(normalized, 1.0)))

    def _build_current_template_report_data(self, run: Run) -> Dict[str, Any]:
        total_requests = getattr(run, "total_requests", None)
        run_status = str(getattr(run.run_status, "value", run.run_status) or "")
        success_rate = self._normalize_ratio(getattr(run, "success_rate", None))
        error_rate = self._normalize_ratio(getattr(run, "error_rate", None))
        if success_rate is None and error_rate is not None:
            success_rate = max(0.0, min(1.0, 1 - error_rate))
        if error_rate is None and success_rate is not None:
            error_rate = max(0.0, min(1.0, 1 - success_rate))
        if error_rate is None and run_status.lower() in {"succeeded", "completed"}:
            error_rate = 0.0
            success_rate = 1.0
        successful_requests = self._derive_request_count(total_requests, success_rate)
        if successful_requests is None:
            failed_from_error_rate = self._derive_request_count(
                total_requests, error_rate
            )
            if isinstance(total_requests, int) and isinstance(
                failed_from_error_rate, int
            ):
                successful_requests = max(
                    0, int(total_requests) - failed_from_error_rate
                )
        failed_requests = (
            int(total_requests) - successful_requests
            if isinstance(total_requests, int)
            and isinstance(successful_requests, int)
            and int(total_requests) >= successful_requests
            else None
        )
        return {
            "run_id": int(run.run_id),
            "task_id": int(run.task_id),
            "task_name": run.task_name,
            "engine_type": str(getattr(run.engine_type, "value", run.engine_type)),
            "protocol_display": self._resolve_run_protocol_display(run),
            "run_status": run_status,
            "total_requests": total_requests,
            "successful_requests": successful_requests,
            "failed_requests": failed_requests,
            "success_rate": success_rate,
            "error_rate": error_rate,
            "rps": getattr(run, "rps", None),
            "rt_avg_ms": getattr(run, "avg_rt_ms", None),
            "rt_p95_ms": getattr(run, "p95_rt_ms", None),
            "rt_p99_ms": getattr(run, "p99_rt_ms", None),
            "test_config": run.params if isinstance(run.params, dict) else {},
        }

    @staticmethod
    def _normalize_protocol_token(value: Any) -> Optional[str]:
        token = str(getattr(value, "value", value) or "").strip().lower()
        if not token:
            return None
        if token == "iteration":
            return "grpc"
        if token == "mixed":
            return "mixed"
        return token

    def _collect_run_protocol_tokens(
        self, run: Optional[Run], data: Dict[str, Any]
    ) -> list[str]:
        protocols: list[str] = []

        def add(value: Any) -> None:
            if isinstance(value, str) and "/" in value:
                for part in value.split("/"):
                    add(part)
                return
            token = self._normalize_protocol_token(value)
            if token and token not in protocols:
                protocols.append(token)

        if run is not None:
            add(getattr(run, "protocol", None))
            params = run.params if isinstance(run.params, dict) else {}
            task = (
                self.repo.db.query(Task).filter(Task.id == run.task_id).first()
                if getattr(run, "task_id", None)
                else None
            )
            if task is not None and isinstance(task.protocols, list):
                for item in task.protocols:
                    add(item)
        else:
            params = {}

        test_config = data.get("test_config")
        if isinstance(test_config, dict):
            params = {**params, **test_config}

        for key in ("protocols", "current_task_protocols"):
            raw_values = params.get(key)
            if isinstance(raw_values, list):
                for item in raw_values:
                    add(item)

        add(params.get("protocol"))
        add(data.get("protocol"))
        add(data.get("protocol_display"))

        k6_summary = params.get("k6_summary")
        metric_family = (
            k6_summary.get("metric_family") if isinstance(k6_summary, dict) else None
        )
        add(metric_family)

        summary_rows = params.get("summary_metrics")
        if isinstance(summary_rows, list):
            has_http = any(
                isinstance(item, dict)
                and str(item.get("endpoint_name") or "").startswith(
                    ("GET ", "POST ", "PUT ", "DELETE ", "PATCH ")
                )
                for item in summary_rows
            )
            has_grpc = any(
                isinstance(item, dict)
                and str(item.get("endpoint_name") or "").startswith("hello.")
                for item in summary_rows
            )
            if has_http:
                add("http")
            if has_grpc:
                add("grpc")

        if "mixed" in protocols and len([p for p in protocols if p != "mixed"]) == 0:
            return ["http", "grpc"]
        return [item for item in protocols if item != "mixed"] or protocols

    def _resolve_run_protocol_display(
        self, run: Optional[Run], data: Optional[Dict[str, Any]] = None
    ) -> str:
        protocols = self._collect_run_protocol_tokens(run, data or {})
        order = ["http", "grpc", "websocket", "kafka", "browser"]
        display_names = {
            "http": "HTTP",
            "grpc": "GRPC",
            "websocket": "WEBSOCKET",
            "kafka": "KAFKA",
            "browser": "BROWSER",
        }
        ordered = [item for item in order if item in protocols]
        ordered.extend(
            item for item in protocols if item not in ordered and item != "mixed"
        )
        if not ordered and "mixed" in protocols:
            ordered = ["http", "grpc"]
        return " / ".join(display_names.get(item, item.upper()) for item in ordered)

    def _create_current_template_report_record(
        self, run: Run, *, user_id: Optional[int] = None
    ) -> Report:
        return self.create_report(
            ReportCreate(
                name=f"测试报告 - {run.task_name or run.task_id}",
                description=f"Run {run.run_id} 的性能测试报告",
                report_type=self._run_report_type(run),
                task_id=int(run.task_id),
                run_id=int(run.run_id),
                test_config=run.params if isinstance(run.params, dict) else None,
            ),
            user_id=user_id,
        )

    def _submit_current_template_report_task(
        self,
        run: Run,
        *,
        user_id: Optional[int],
        message_prefix: str,
    ) -> Dict[str, Any]:
        self._ensure_run_terminal_for_report(run, action="生成报告")
        report = self._create_current_template_report_record(run, user_id=user_id)
        report_data = self._build_current_template_report_data(run)
        try:
            from app.tasks.report_generator import generate_report_task

            task = generate_report_task.delay(int(report.id), report_data)
        except Exception as exc:  # pragma: no cover - enqueue failure
            self.mark_report_failed(int(report.id), f"enqueue failed: {exc}")
            raise
        return {
            "status": "generating",
            "message": f"{message_prefix} #{report.id}",
            "report_id": int(report.id),
            "report_name": report.name,
            "generated": True,
            "accepted": True,
            "completed": False,
            "async_task_id": str(task.id),
            "ai_report": self._load_latest_ai_report_metadata(int(run.run_id)),
        }

    def _create_current_template_report(
        self, run: Run, *, user_id: Optional[int] = None
    ) -> Report:
        """创建并生成一份当前模板的单 Run HTML 报告。"""
        self._ensure_run_terminal_for_report(run, action="生成报告")
        report = self._create_current_template_report_record(run, user_id=user_id)
        self.generate_report_file(
            int(report.id), self._build_current_template_report_data(run)
        )
        refreshed_report = self.get_report(int(report.id))
        return refreshed_report or report

    def _load_completed_reports_by_run_ids(
        self, run_ids: List[int]
    ) -> Dict[int, List[Report]]:
        reports = (
            self.repo.db.query(Report)
            .filter(
                Report.run_id.in_(run_ids),
                Report.status == ReportStatus.COMPLETED,
            )
            .order_by(Report.run_id.asc(), Report.created_at.desc(), Report.id.desc())
            .all()
        )
        grouped: Dict[int, List[Report]] = {run_id: [] for run_id in run_ids}
        for report in reports:
            if report.run_id is None:
                continue
            grouped.setdefault(int(report.run_id), []).append(report)
        return grouped

    def _load_latest_ai_report_metadata_by_run_ids(
        self, run_ids: List[int]
    ) -> Dict[int, Dict[str, Any]]:
        del run_ids
        return {}

    def list_reports(
        self,
        skip: int = 0,
        limit: int = 100,
        task_id: Optional[int] = None,
        run_id: Optional[int] = None,
        status: Optional[ReportStatus] = None,
        report_type: Optional[ReportType] = None,
    ) -> Tuple[List[Report], int]:
        """获取报告列表"""
        reports = self.repo.get_multi(
            skip=skip,
            limit=limit,
            task_id=task_id,
            run_id=run_id,
            status=status,
            report_type=report_type,
        )
        total = self.repo.count(
            task_id=task_id, run_id=run_id, status=status, report_type=report_type
        )
        return reports, total

    def update_report(
        self, report_id: int, report_in: ReportUpdate
    ) -> Optional[Report]:
        """更新报告"""
        update_data = report_in.model_dump(exclude_unset=True)
        return self.repo.update(report_id, update_data)

    def delete_report(self, report_id: int) -> bool:
        """删除报告"""
        return self.repo.delete(report_id)

    def search_reports(
        self, keyword: str, skip: int = 0, limit: int = 100
    ) -> List[Report]:
        """搜索报告"""
        return self.repo.search(keyword, skip, limit)

    def get_statistics(self) -> ReportStatistics:
        """获取统计信息"""
        stats = self.repo.get_statistics()
        return ReportStatistics(**stats)

    @staticmethod
    def _first_non_none(*values):
        for value in values:
            if value is not None:
                return value
        return None

    @staticmethod
    def _numeric_value(value: Any) -> Optional[float]:
        if isinstance(value, bool) or value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value))
        except (TypeError, ValueError):
            return None

    def _normalize_ratio(self, value: Any) -> Optional[float]:
        numeric = self._numeric_value(value)
        if numeric is None or numeric < 0:
            return None
        normalized = numeric if numeric <= 1 else numeric / 100.0
        return max(0.0, min(1.0, normalized))

    def _has_successful_terminal_context(
        self,
        report_data: Dict[str, Any],
        summary: Dict[str, Any],
    ) -> bool:
        raw_status = self._first_non_none(
            report_data.get("run_status"),
            report_data.get("status"),
            summary.get("run_status"),
            summary.get("status"),
        )
        normalized_status = (
            str(getattr(raw_status, "value", raw_status) or "").strip().lower()
        )
        if normalized_status in {"succeeded", "success", "passed", "completed"}:
            return True

        check_rows = self._coerce_rows(report_data.get("checks")) or self._coerce_rows(
            summary.get("checks")
        )
        if not check_rows:
            return False
        rates = [
            self._normalize_ratio(row.get("success_rate"))
            for row in check_rows
            if row.get("success_rate") is not None
        ]
        return len(rates) == len(check_rows) and all(
            rate is not None and rate >= 1.0 for rate in rates
        )

    def _build_summary_metrics_overall_fields(
        self, report_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        rows = self._coerce_rows(report_data.get("summary_metrics"))
        if not rows:
            return {}

        total_requests = 0
        total_throughput = 0.0
        has_total_requests = False
        has_throughput = False

        for row in rows:
            row_total = self._numeric_value(row.get("total_requests"))
            if row_total is not None and row_total > 0:
                total_requests += int(row_total)
                has_total_requests = True
            row_throughput = self._numeric_value(row.get("throughput"))
            if row_throughput is not None and row_throughput > 0:
                total_throughput += row_throughput
                has_throughput = True

        def weighted_average(field: str) -> Optional[float]:
            metric_rows = [
                row for row in rows if self._numeric_value(row.get(field)) is not None
            ]
            if not metric_rows:
                return None
            weighted_rows = [
                row
                for row in metric_rows
                if (self._numeric_value(row.get("total_requests")) or 0) > 0
            ]
            if weighted_rows:
                weight_total = sum(
                    self._numeric_value(row.get("total_requests")) or 0
                    for row in weighted_rows
                )
                if weight_total > 0:
                    return (
                        sum(
                            (self._numeric_value(row.get(field)) or 0)
                            * (self._numeric_value(row.get("total_requests")) or 0)
                            for row in weighted_rows
                        )
                        / weight_total
                    )
            return sum(
                self._numeric_value(row.get(field)) or 0 for row in metric_rows
            ) / len(metric_rows)

        return {
            "total_requests": total_requests if has_total_requests else None,
            "throughput": total_throughput if has_throughput else None,
            "avg_response_time": weighted_average("avg_rt_ms"),
            "p95_response_time": weighted_average("p95_rt_ms"),
            "p99_response_time": weighted_average("p99_rt_ms"),
        }

    def _build_numeric_fields(self, report_data: Dict[str, Any]) -> Dict[str, Any]:
        jtl = report_data.get("jtl_summary") or {}
        k6 = report_data.get("k6_summary") or {}
        summary = jtl or k6
        summary_metrics = self._build_summary_metrics_overall_fields(report_data)
        fields = {
            "total_requests": self._first_non_none(
                report_data.get("total_requests"),
                summary.get("total_requests"),
                summary_metrics.get("total_requests"),
            ),
            "successful_requests": self._first_non_none(
                report_data.get("successful_requests"),
                summary.get("successful_requests"),
            ),
            "failed_requests": self._first_non_none(
                report_data.get("failed_requests"),
                summary.get("failed_requests"),
            ),
            "error_rate": self._first_non_none(
                summary.get("error_rate"),
                report_data.get("error_rate"),
            ),
            "avg_response_time": self._first_non_none(
                summary.get("avg_response_time"),
                summary.get("rt_avg_ms"),
                report_data.get("rt_avg_ms"),
                summary_metrics.get("avg_response_time"),
            ),
            "p95_response_time": self._first_non_none(
                summary.get("p95_response_time"),
                summary.get("rt_p95_ms"),
                report_data.get("rt_p95_ms"),
                summary_metrics.get("p95_response_time"),
            ),
            "p99_response_time": self._first_non_none(
                summary.get("p99_response_time"),
                summary.get("rt_p99_ms"),
                report_data.get("rt_p99_ms"),
                summary_metrics.get("p99_response_time"),
            ),
            "throughput": self._first_non_none(
                summary.get("throughput"),
                summary.get("http_reqs"),
                report_data.get("rps"),
                summary_metrics.get("throughput"),
            ),
        }
        total_requests = fields.get("total_requests")
        error_rate = self._normalize_ratio(fields.get("error_rate"))
        success_rate = self._normalize_ratio(
            self._first_non_none(
                report_data.get("success_rate"),
                summary.get("success_rate"),
            )
        )
        if error_rate is None and success_rate is not None:
            error_rate = max(0.0, min(1.0, 1 - success_rate))
        fields["error_rate"] = error_rate
        if fields.get("failed_requests") is None:
            fields["failed_requests"] = self._derive_request_count(
                total_requests, error_rate
            )
        if fields.get("successful_requests") is None:
            failed_requests = fields.get("failed_requests")
            if isinstance(total_requests, (int, float)) and isinstance(
                failed_requests, int
            ):
                fields["successful_requests"] = max(
                    0, int(round(float(total_requests))) - failed_requests
                )
            else:
                fields["successful_requests"] = self._derive_request_count(
                    total_requests, success_rate
                )
        if fields.get("error_rate") is None and isinstance(
            total_requests, (int, float)
        ):
            failed_requests = fields.get("failed_requests")
            successful_requests = fields.get("successful_requests")
            total = int(round(float(total_requests)))
            if total > 0 and isinstance(failed_requests, int):
                fields["error_rate"] = max(0.0, min(1.0, failed_requests / total))
            elif total > 0 and isinstance(successful_requests, int):
                fields["error_rate"] = max(
                    0.0, min(1.0, (total - successful_requests) / total)
                )
            elif total > 0 and self._has_successful_terminal_context(
                report_data, summary
            ):
                fields["error_rate"] = 0.0
                fields["failed_requests"] = 0
                fields["successful_requests"] = total
        if fields.get("failed_requests") is None:
            fields["failed_requests"] = self._derive_request_count(
                total_requests, fields.get("error_rate")
            )
        if fields.get("successful_requests") is None:
            failed_requests = fields.get("failed_requests")
            if isinstance(total_requests, (int, float)) and isinstance(
                failed_requests, int
            ):
                fields["successful_requests"] = max(
                    0, int(round(float(total_requests))) - failed_requests
                )
        return fields

    @staticmethod
    def _overlay_terminal_summary_fields(report_data: Dict[str, Any]) -> Dict[str, Any]:
        enriched = dict(report_data)
        jtl = enriched.get("jtl_summary") or {}
        k6 = enriched.get("k6_summary") or {}
        summary = k6 or jtl
        if not isinstance(summary, dict):
            return enriched

        assigned_targets: set[str] = set()
        for source_key, target_key in (
            ("throughput", "rps"),
            ("http_reqs", "rps"),
            ("rt_avg_ms", "rt_avg_ms"),
            ("avg_response_time", "rt_avg_ms"),
            ("rt_p95_ms", "rt_p95_ms"),
            ("p95_response_time", "rt_p95_ms"),
            ("rt_p99_ms", "rt_p99_ms"),
            ("p99_response_time", "rt_p99_ms"),
            ("error_rate", "error_rate"),
        ):
            value = summary.get(source_key)
            if value is None or target_key in assigned_targets:
                continue
            enriched[target_key] = value
            assigned_targets.add(target_key)
        return enriched

    @staticmethod
    def _coerce_rows(value: Any) -> List[Dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [row for row in value if isinstance(row, dict)]

    @staticmethod
    def _escape(value: Any) -> str:
        if value is None:
            return "-"
        return html.escape(str(value))

    @staticmethod
    def _format_number(value: Any, decimals: int = 2) -> str:
        if value in (None, ""):
            return "-"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            if value.is_integer():
                return str(int(value))
            return f"{value:.{decimals}f}".rstrip("0").rstrip(".")
        return str(value)

    @classmethod
    def _format_percent(cls, value: Any) -> str:
        if value in (None, ""):
            return "-"
        if isinstance(value, (int, float)):
            normalized = value * 100 if 0 <= value <= 1 else value
            formatted = f"{normalized:.2f}".rstrip("0").rstrip(".")
            return f"{formatted}%"
        return cls._escape(value)

    @classmethod
    def _format_observability_metrics(cls, metrics: Any) -> str:
        if not isinstance(metrics, dict):
            return "-"
        parts: list[str] = []
        for key, raw in metrics.items():
            if not isinstance(raw, dict):
                continue
            samples = int(raw.get("samples") or 0)
            value = raw.get("value")
            if samples <= 0 or value is None:
                continue
            parts.append(f"{key}={cls._format_number(value)}")
            if len(parts) >= 6:
                break
        return "；".join(parts) if parts else "无样本"

    @classmethod
    def _format_datetime(cls, value: Any) -> str:
        if value is None:
            return "-"
        if isinstance(value, datetime):
            return cls._to_display_datetime(value).strftime("%Y-%m-%d %H:%M:%S %Z")
        return cls._escape(value)

    @staticmethod
    def _to_display_datetime(value: datetime) -> datetime:
        normalized = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return normalized.astimezone(REPORT_DISPLAY_TIMEZONE)

    @classmethod
    def _format_trend_time(cls, value: datetime) -> str:
        return cls._to_display_datetime(value).strftime("%H:%M:%S")

    def _load_run(self, report: Report) -> Optional[Run]:
        if report.run_id is None:
            return None
        return self.repo.db.query(Run).filter(Run.run_id == report.run_id).first()

    def _load_latest_ai_report_metadata(
        self, run_id: Optional[int]
    ) -> Optional[Dict[str, Any]]:
        del run_id
        return None

    def _build_ai_report_metadata(self, latest: Any) -> Dict[str, Any]:
        del latest
        return {}

    @staticmethod
    def _safe_string_list(value: Any) -> List[str]:
        if value is None:
            return []
        items = value if isinstance(value, list) else [value]
        safe_items = []
        for item in items:
            if item is None:
                continue
            text = str(item).strip()
            if not text:
                continue
            lowered = text.lower()
            if any(
                marker in lowered
                for marker in (
                    "ai_api_key",
                    "api_key",
                    "apikey",
                    "authorization:",
                    "bearer ",
                    "secret",
                    "password",
                    "token=",
                )
            ):
                continue
            safe_items.append(text)
        return safe_items

    def _render_meta_item(self, label: str, value: Any) -> str:
        return (
            '<div class="meta-item">'
            f'<span class="meta-label">{self._escape(label)}</span>'
            f'<span class="meta-value">{self._escape(value)}</span>'
            "</div>"
        )

    def _render_card(self, label: str, value: Any, suffix: str = "") -> str:
        return (
            '<div class="metric-card">'
            f'<div class="metric-value">{self._escape(self._format_number(value))}{self._escape(suffix)}</div>'
            f'<div class="metric-label">{self._escape(label)}</div>'
            "</div>"
        )

    def _render_table(
        self, columns: List[Tuple[str, str]], rows: List[Dict[str, Any]]
    ) -> str:
        if not rows:
            return '<div class="empty-state">当前暂无可展示数据</div>'
        header_html = "".join(f"<th>{self._escape(label)}</th>" for label, _ in columns)
        body_html = "".join(
            "<tr>"
            + "".join(
                f"<td>{self._escape(row.get(key, '-'))}</td>" for _, key in columns
            )
            + "</tr>"
            for row in rows
        )
        return (
            '<div class="table-wrap">'
            "<table>"
            f"<thead><tr>{header_html}</tr></thead>"
            f"<tbody>{body_html}</tbody>"
            "</table>"
            "</div>"
        )

    def _render_ai_list(self, items: List[str], empty_message: str) -> str:
        if not items:
            return f'<div class="empty-state">{self._escape(empty_message)}</div>'
        return (
            '<ul class="ai-list">'
            + "".join(f"<li>{self._escape(item)}</li>" for item in items)
            + "</ul>"
        )

    def _render_ai_report_section(
        self, ai_report_metadata: Optional[Dict[str, Any]]
    ) -> str:
        metadata = ai_report_metadata or {}
        summary = metadata.get("summary")
        hypotheses = self._safe_string_list(metadata.get("root_cause_hypotheses"))
        evidence_references = self._safe_string_list(
            metadata.get("evidence_references")
        )
        limitations = self._safe_string_list(metadata.get("limitations"))
        disclaimer = metadata.get("disclaimer")

        if metadata:
            heading = (
                f'AI Report #{self._escape(metadata.get("report_id"))}'
                f' · {self._escape(metadata.get("status") or "-")}'
            )
            meta_line = (
                f'Prompt {self._escape(metadata.get("prompt_version") or "-")}'
                f' · {self._escape(metadata.get("provider") or "-")}'
                f' / {self._escape(metadata.get("model") or "-")}'
            )
        else:
            heading = "当前 Run 暂无 AI Report 元数据"
            meta_line = "AI 摘要、根因假设、证据引用与限制说明将在生成后展示。"

        summary_html = (
            f'<div class="ai-summary-text">{self._escape(summary)}</div>'
            if summary
            else '<div class="empty-state">当前暂无 AI 摘要。</div>'
        )
        disclaimer_html = (
            f'<div class="ai-disclaimer">{self._escape(disclaimer)}</div>'
            if disclaimer
            else ""
        )

        return (
            '<div class="ai-summary">'
            f'<div class="ai-summary-box"><strong>{heading}</strong>'
            f"<span>{meta_line}</span></div>"
            '<div class="ai-panel"><h3>AI 摘要</h3>'
            f"{summary_html}</div>"
            '<div class="ai-panel"><h3>根因假设</h3>'
            f"{self._render_ai_list(hypotheses, '当前暂无根因假设。')}</div>"
            '<div class="ai-panel"><h3>证据引用</h3>'
            f"{self._render_ai_list(evidence_references, '当前暂无证据引用。')}</div>"
            '<div class="ai-panel"><h3>限制说明</h3>'
            f"{self._render_ai_list(limitations, '当前暂无限制说明。')}"
            f"{disclaimer_html}</div>"
            "</div>"
        )

    @staticmethod
    def _parse_dt(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None

    def _serialize_response_items(self, items: Any) -> List[Dict[str, Any]]:
        serialized: List[Dict[str, Any]] = []
        if not isinstance(items, list):
            return serialized
        for item in items:
            if hasattr(item, "model_dump"):
                serialized.append(item.model_dump(mode="json"))
            elif isinstance(item, dict):
                serialized.append(item)
        return serialized

    def _enrich_report_data(
        self, report: Report, report_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        enriched = self._overlay_terminal_summary_fields(report_data)
        enriched["template_version"] = REPORT_TEMPLATE_VERSION
        enriched["timezone"] = REPORT_DISPLAY_TIMEZONE_NAME
        run_id = enriched.get("run_id") or report.run_id
        if run_id is None:
            return enriched

        try:
            parsed_run_id = int(run_id)
        except (TypeError, ValueError):
            return enriched

        run_service = RunService(self.repo.db)

        if not self._coerce_rows(enriched.get("summary_metrics")):
            try:
                summary = run_service.get_summary_metrics(parsed_run_id)
                enriched["summary_metrics"] = self._serialize_response_items(
                    summary.items
                )
            except Exception as exc:  # pragma: no cover - 容错
                logger.warning(
                    "failed to enrich summary_metrics for report %s run %s: %s",
                    report.id,
                    parsed_run_id,
                    exc,
                )

        if not self._coerce_rows(enriched.get("checks")):
            try:
                checks = run_service.get_checks(parsed_run_id)
                enriched["checks"] = self._serialize_response_items(checks.items)
            except Exception as exc:  # pragma: no cover - 容错
                logger.warning(
                    "failed to enrich checks for report %s run %s: %s",
                    report.id,
                    parsed_run_id,
                    exc,
                )

        endpoint_trends = enriched.get("endpoint_trends")
        trend_map = dict(endpoint_trends) if isinstance(endpoint_trends, dict) else {}
        for metric in ("throughput", "rt_avg_ms", "rt_p95_ms", "rt_p99_ms"):
            if metric in trend_map:
                continue
            try:
                trend_response = run_service.get_endpoint_trends(
                    parsed_run_id, metric=metric
                )
                trend_map[metric] = {
                    "step_seconds": trend_response.step_seconds,
                    "items": self._serialize_response_items(trend_response.items),
                }
            except Exception as exc:  # pragma: no cover - 容错
                logger.warning(
                    "failed to enrich endpoint trend %s for report %s run %s: %s",
                    metric,
                    report.id,
                    parsed_run_id,
                    exc,
                )
        if trend_map:
            enriched["endpoint_trends"] = trend_map

        if not isinstance(enriched.get("ai_report"), dict):
            ai_report_metadata = self._load_latest_ai_report_metadata(parsed_run_id)
            if ai_report_metadata:
                enriched["ai_report"] = ai_report_metadata

        return enriched

    def _render_trend_chart(self, title: str, payload: Any, unit: str) -> str:
        trend_items = []
        if isinstance(payload, dict):
            trend_items = self._coerce_rows(payload.get("items"))
        if not trend_items:
            return (
                '<div class="trend-card">'
                f"<h3>{self._escape(title)}</h3>"
                '<div class="empty-state">当前暂无可展示曲线</div>'
                "</div>"
            )

        normalized_rows: List[Dict[str, Any]] = []
        for row in trend_items:
            endpoint_name = row.get("endpoint_name") or "-"
            points = []
            for point in row.get("points") or []:
                if not isinstance(point, dict):
                    continue
                ts = self._parse_dt(point.get("ts"))
                value = point.get("value")
                if ts is None or not isinstance(value, (int, float)):
                    continue
                points.append({"ts": ts, "value": float(value)})
            if points:
                normalized_rows.append(
                    {"endpoint_name": endpoint_name, "points": points}
                )

        if not normalized_rows:
            return (
                '<div class="trend-card">'
                f"<h3>{self._escape(title)}</h3>"
                '<div class="empty-state">当前暂无可展示曲线</div>'
                "</div>"
            )

        width = 920
        height = 240
        left = 52
        right = 18
        top = 16
        bottom = 32
        usable_width = width - left - right
        usable_height = height - top - bottom
        palette = ["#1d4ed8", "#ea580c", "#0f766e", "#b91c1c", "#7c3aed", "#047857"]

        timestamps = sorted(
            {point["ts"] for row in normalized_rows for point in row["points"]}
        )
        values = [point["value"] for row in normalized_rows for point in row["points"]]
        min_value = min(values)
        max_value = max(values)
        if min_value == max_value:
            min_value -= 1
            max_value += 1

        min_ts = timestamps[0]
        max_ts = timestamps[-1]
        total_seconds = max((max_ts - min_ts).total_seconds(), 1)

        def x_pos(ts: datetime) -> float:
            return left + (
                ((ts - min_ts).total_seconds() / total_seconds) * usable_width
            )

        def y_pos(value: float) -> float:
            ratio = (value - min_value) / (max_value - min_value)
            return top + ((1 - ratio) * usable_height)

        grid_lines = []
        for index in range(4):
            y = top + (usable_height * index / 3)
            grid_lines.append(
                f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" class="trend-grid-line" />'
            )

        series_lines = []
        legend_items = []
        for index, row in enumerate(normalized_rows):
            color = palette[index % len(palette)]
            polyline = " ".join(
                f"{x_pos(point['ts']):.1f},{y_pos(point['value']):.1f}"
                for point in row["points"]
            )
            series_lines.append(
                f'<polyline fill="none" stroke="{color}" stroke-width="2.5" points="{polyline}" />'
            )
            legend_items.append(
                '<span class="trend-legend-item">'
                f'<span class="trend-legend-dot" style="background:{color}"></span>'
                f'{self._escape(row["endpoint_name"])}'
                "</span>"
            )

        axis_labels = [
            f'<text x="{left}" y="{height-10}" class="trend-axis-label">{self._escape(self._format_trend_time(min_ts))}</text>',
            f'<text x="{width-right}" y="{height-10}" text-anchor="end" class="trend-axis-label">{self._escape(self._format_trend_time(max_ts))}</text>',
            f'<text x="{left-8}" y="{top+10}" text-anchor="end" class="trend-axis-label">{self._escape(self._format_number(max_value))}</text>',
            f'<text x="{left-8}" y="{height-bottom+4}" text-anchor="end" class="trend-axis-label">{self._escape(self._format_number(min_value))}</text>',
        ]

        return (
            '<div class="trend-card">'
            f"<h3>{self._escape(title)}</h3>"
            f'<div class="trend-unit">{self._escape(unit)}</div>'
            f'<svg viewBox="0 0 {width} {height}" class="trend-chart" role="img" aria-label="{self._escape(title)}">'
            + "".join(grid_lines)
            + "".join(series_lines)
            + "".join(axis_labels)
            + "</svg>"
            f'<div class="trend-legend">{"".join(legend_items)}</div>'
            "</div>"
        )

    def generate_report_file(self, report_id: int, report_data: Dict[str, Any]) -> str:
        """生成报告文件"""
        report = self.get_report(report_id)
        if not report:
            raise ValueError(f"Report {report_id} not found")

        guard_run_ids: list[int] = []
        for raw_run_id in (report.run_id, report_data.get("run_id")):
            if raw_run_id is None:
                continue
            try:
                guard_run_id = int(raw_run_id)
            except (TypeError, ValueError):
                continue
            if guard_run_id not in guard_run_ids:
                guard_run_ids.append(guard_run_id)
        for guard_run_id in guard_run_ids:
            run = self.repo.db.query(Run).filter(Run.run_id == guard_run_id).first()
            if run is not None:
                self._ensure_run_terminal_for_report(run, action="生成报告")

        report_data = self._enrich_report_data(report, report_data)

        # 更新报告状态
        numeric_fields = self._build_numeric_fields(report_data)
        run_id = report_data.get("run_id")
        generating_update = {
            "status": ReportStatus.GENERATING,
            "metrics_data": report_data,
            **{k: v for k, v in numeric_fields.items() if v is not None},
        }
        if run_id is not None:
            generating_update["run_id"] = run_id
        self.repo.update(report_id, generating_update)

        # 生成报告内容
        report_content = self._format_report_content(report, report_data)

        # 保存到本地临时文件
        local_file_path = self.reports_dir / f"report_{report_id}.html"
        with open(local_file_path, "w", encoding="utf-8") as f:
            f.write(report_content)

        file_size = local_file_path.stat().st_size
        final_file_path = str(local_file_path)

        # 如果开启了 S3 归档，则推送到 S3
        use_s3 = os.getenv("USE_S3", "0") == "1"
        bucket = os.getenv("S3_BUCKET") or settings.S3_BUCKET
        if use_s3 and bucket:
            prefix = get_report_artifact_prefix()
            s3_key = f"{prefix}/report_{report_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.html"
            try:
                s3_utils.upload_bytes(
                    bucket,
                    s3_key,
                    local_file_path.read_bytes(),
                    content_type="text/html",
                )
                final_file_path = f"s3://{bucket}/{s3_key}"
            except Exception as e:
                logger.warning(
                    f"Failed to upload report {report_id} to S3 ({bucket}/{s3_key}): {e}, falling back to local file."
                )
                pass  # S3 失败时降级使用本地路径

        # 更新报告状态和文件信息
        final_update = {
            "status": ReportStatus.COMPLETED,
            "file_path": final_file_path,
            "file_size": file_size,
            "generated_at": datetime.now(timezone.utc),
            **{k: v for k, v in numeric_fields.items() if v is not None},
        }
        if report_data.get("run_id") is not None:
            final_update["run_id"] = report_data["run_id"]
        self.repo.update(report_id, final_update)

        return final_file_path

    def get_report_file_path(
        self, report_id: int, *, regenerate_missing: bool = False
    ) -> Path:
        report = self.get_report(report_id)
        if report is not None and report.run_id is not None:
            run = self.repo.db.query(Run).filter(Run.run_id == report.run_id).first()
            if run is not None:
                self._ensure_run_terminal_for_report(run, action="下载报告")
        if regenerate_missing and report is not None and report.run_id is not None:
            try:
                if not self._report_file_matches_current_template(report):
                    run = (
                        self.repo.db.query(Run)
                        .filter(Run.run_id == report.run_id)
                        .first()
                    )
                    if run is not None:
                        self.generate_report_file(
                            int(report.id),
                            self._build_current_template_report_data(run),
                        )
                        refreshed = self.get_report(report_id)
                        return self._resolve_report_file_path(refreshed)
            except FileNotFoundError:
                pass
        try:
            return self._resolve_report_file_path(report)
        except FileNotFoundError:
            if not regenerate_missing or report is None or report.run_id is None:
                raise
            run = self.repo.db.query(Run).filter(Run.run_id == report.run_id).first()
            if run is None:
                raise
            self.generate_report_file(
                int(report.id), self._build_current_template_report_data(run)
            )
            refreshed = self.get_report(report_id)
            return self._resolve_report_file_path(refreshed)

    def _resolve_report_file_path(self, report: Optional[Report]) -> Path:
        if not report or not report.file_path:
            raise FileNotFoundError("Report file not found")

        file_path_str = report.file_path

        # 如果是 S3 路径，则先下载到本地临时目录
        if file_path_str.startswith("s3://"):
            parts = file_path_str.replace("s3://", "").split("/", 1)
            if len(parts) == 2:
                bucket, key = parts[0], parts[1]
                local_path = self.reports_dir / f"cached_report_{report.id}.html"
                try:
                    data = s3_utils.download_bytes(bucket, key)
                    local_path.write_bytes(data)
                    return local_path
                except Exception as e:
                    raise FileNotFoundError(f"Failed to download report from S3: {e}")

        path = Path(file_path_str)
        if not path.exists():
            raise FileNotFoundError("Report file not found")
        return path

    def _format_report_content(self, report: Report, data: Dict[str, Any]) -> str:
        """格式化报告内容为 HTML"""
        jtl = data.get("jtl_summary") or {}
        k6 = data.get("k6_summary") or {}
        numeric_fields = self._build_numeric_fields(data)
        run = self._load_run(report)
        run_params = run.params if run and isinstance(run.params, dict) else {}
        ai_report_metadata = (
            data.get("ai_report")
            if isinstance(data.get("ai_report"), dict)
            else self._load_latest_ai_report_metadata(report.run_id)
        )
        summary_metrics = self._coerce_rows(
            data.get("summary_metrics")
        ) or self._coerce_rows(run_params.get("summary_metrics"))
        checks = (
            self._coerce_rows(data.get("checks"))
            or self._coerce_rows(run_params.get("checks"))
            or self._coerce_rows(k6.get("checks"))
        )
        agent_runs = self._coerce_rows(data.get("agent_runs")) or self._coerce_rows(
            run_params.get("agent_runs")
        )
        trend_map = (
            data.get("endpoint_trends")
            if isinstance(data.get("endpoint_trends"), dict)
            else {}
        )
        observability_analysis = ReportObservabilityAnalysisService(
            self.repo.db
        ).build_for_run(run)
        data["observability_analysis"] = observability_analysis

        overall_cards = [
            self._render_card(
                "总请求数",
                self._first_non_none(
                    report.total_requests,
                    numeric_fields.get("total_requests"),
                    jtl.get("total_requests"),
                    k6.get("total_requests"),
                ),
            ),
            self._render_card(
                "成功请求",
                self._first_non_none(
                    report.successful_requests,
                    numeric_fields.get("successful_requests"),
                    jtl.get("successful_requests"),
                    k6.get("successful_requests"),
                ),
            ),
            self._render_card(
                "失败请求",
                self._first_non_none(
                    report.failed_requests,
                    numeric_fields.get("failed_requests"),
                    jtl.get("failed_requests"),
                    k6.get("failed_requests"),
                ),
            ),
            self._render_card(
                "吞吐量",
                self._first_non_none(
                    report.throughput,
                    numeric_fields.get("throughput"),
                    jtl.get("throughput"),
                    k6.get("throughput"),
                ),
                " req/s",
            ),
            self._render_card(
                "平均响应时间",
                self._first_non_none(
                    report.avg_response_time,
                    numeric_fields.get("avg_response_time"),
                    jtl.get("avg_response_time"),
                    jtl.get("rt_avg_ms"),
                    k6.get("avg_response_time"),
                    k6.get("rt_avg_ms"),
                ),
                " ms",
            ),
            self._render_card(
                "P95",
                self._first_non_none(
                    report.p95_response_time,
                    numeric_fields.get("p95_response_time"),
                    jtl.get("p95_response_time"),
                    k6.get("p95_response_time"),
                ),
                " ms",
            ),
            self._render_card(
                "P99",
                self._first_non_none(
                    report.p99_response_time,
                    numeric_fields.get("p99_response_time"),
                    jtl.get("p99_response_time"),
                    k6.get("p99_response_time"),
                ),
                " ms",
            ),
            self._render_card(
                "错误率",
                self._format_percent(
                    self._first_non_none(
                        report.error_rate,
                        numeric_fields.get("error_rate"),
                        jtl.get("error_rate"),
                        k6.get("error_rate"),
                    )
                ),
            ),
        ]

        endpoint_rows = [
            {
                "endpoint_name": row.get("endpoint_name", "-"),
                "total_requests": self._format_number(row.get("total_requests")),
                "throughput": f"{self._format_number(row.get('throughput'))} req/s",
                "avg_rt_ms": f"{self._format_number(row.get('avg_rt_ms'))} ms",
                "p95_rt_ms": f"{self._format_number(row.get('p95_rt_ms'))} ms",
                "p99_rt_ms": f"{self._format_number(row.get('p99_rt_ms'))} ms",
            }
            for row in summary_metrics
        ]
        checks_rows = [
            {
                "group_name": row.get("group_name") or "-",
                "check_name": row.get("check_name") or "-",
                "success_rate": self._format_percent(row.get("success_rate")),
            }
            for row in checks
        ]
        error_rows = []
        if (
            report.failed_requests
            or jtl.get("failed_requests")
            or k6.get("failed_requests")
        ):
            error_rows.append(
                {
                    "summary": "失败请求",
                    "detail": self._format_number(
                        self._first_non_none(
                            report.failed_requests,
                            jtl.get("failed_requests"),
                            k6.get("failed_requests"),
                        )
                    ),
                }
            )
        if run and run.stop_reason:
            error_rows.append({"summary": "停止原因", "detail": run.stop_reason})
        if isinstance(data.get("error"), str) and data.get("error"):
            error_rows.append({"summary": "生成错误", "detail": data.get("error")})

        metadata_items = [
            self._render_meta_item("报告 ID", report.id),
            self._render_meta_item("任务 ID", report.task_id),
            self._render_meta_item("Run ID", report.run_id or "-"),
            self._render_meta_item("报告类型", report.report_type.value),
            self._render_meta_item("任务名称", run.task_name if run else report.name),
            self._render_meta_item(
                "引擎 / 协议",
                " / ".join(
                    filter(
                        None,
                        [
                            getattr(run.engine_type, "value", None) if run else None,
                            self._resolve_run_protocol_display(run, data),
                        ],
                    )
                )
                or report.report_type.value,
            ),
            self._render_meta_item("压测环境", run.env if run else "-"),
            self._render_meta_item(
                "运行状态",
                (
                    getattr(run.run_status, "value", "-")
                    if run
                    else data.get("run_status", "-")
                ),
            ),
            self._render_meta_item(
                "开始时间", self._format_datetime(run.started_at) if run else "-"
            ),
            self._render_meta_item(
                "结束时间", self._format_datetime(run.ended_at) if run else "-"
            ),
            self._render_meta_item(
                "时长", f"{self._format_number(run.duration_seconds)} s" if run else "-"
            ),
            self._render_meta_item(
                "Pod 数量",
                run_params.get("pod_count")
                or run_params.get("pod_num")
                or len(agent_runs)
                or "-",
            ),
        ]

        artifact_items = [
            self._render_meta_item(
                "日志归档", data.get("log_s3") or run_params.get("log_s3") or "-"
            ),
            self._render_meta_item(
                "指标归档",
                data.get("metrics_s3") or run_params.get("metrics_s3") or "-",
            ),
            self._render_meta_item(
                "Agent Host",
                data.get("agent_host") or run_params.get("agent_host") or "-",
            ),
            self._render_meta_item(
                "Agent IP", data.get("agent_ip") or run_params.get("agent_ip") or "-"
            ),
        ]
        if agent_runs:
            artifact_items.extend(
                self._render_meta_item(
                    f"Agent #{index + 1}",
                    f"{agent.get('agent_ip') or '-'} / {agent.get('agent_host') or '-'}",
                )
                for index, agent in enumerate(agent_runs)
            )

        trend_cards = [
            self._render_trend_chart(
                "接口 TPS 趋势", trend_map.get("throughput"), "req/s"
            ),
            self._render_trend_chart(
                "接口 Avg 响应时间趋势", trend_map.get("rt_avg_ms"), "ms"
            ),
            self._render_trend_chart(
                "接口 P95 响应时间趋势", trend_map.get("rt_p95_ms"), "ms"
            ),
            self._render_trend_chart(
                "接口 P99 响应时间趋势", trend_map.get("rt_p99_ms"), "ms"
            ),
        ]
        observability_analysis_rows = [
            {
                "title": item.get("title") or item.get("domain") or "-",
                "status": item.get("status") or "-",
                "conclusion": item.get("conclusion") or "-",
                "metrics": self._format_observability_metrics(item.get("metrics")),
            }
            for item in observability_analysis.get("domains", []) or []
        ]
        html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>{self._escape(report.name)}</title>
    <style>
        :root {{
            color-scheme: light;
            --bg: #f4f7fb;
            --panel: #ffffff;
            --line: #dbe3ee;
            --text: #172033;
            --muted: #5c6b83;
            --accent: #1d4ed8;
            --accent-soft: #dbeafe;
            --danger: #b91c1c;
            --danger-soft: #fee2e2;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            padding: 32px;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: linear-gradient(180deg, #f8fbff 0%, var(--bg) 100%);
            color: var(--text);
        }}
        .page {{
            max-width: 1180px;
            margin: 0 auto;
        }}
        .hero {{
            background: linear-gradient(135deg, #10224a 0%, #1d4ed8 100%);
            color: #fff;
            padding: 28px 32px;
            border-radius: 20px;
            box-shadow: 0 20px 50px rgba(15, 23, 42, 0.12);
        }}
        .hero h1 {{
            margin: 0 0 10px;
            font-size: 30px;
        }}
        .hero p {{
            margin: 0;
            color: rgba(255, 255, 255, 0.82);
        }}
        .section {{
            margin-top: 20px;
            background: var(--panel);
            border: 1px solid var(--line);
            border-radius: 18px;
            padding: 24px;
            box-shadow: 0 10px 30px rgba(15, 23, 42, 0.05);
        }}
        .section h2 {{
            margin: 0 0 16px;
            font-size: 20px;
        }}
        .section-note {{
            margin: -8px 0 16px;
            color: var(--muted);
            font-size: 13px;
        }}
        .meta-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 12px;
        }}
        .meta-item {{
            padding: 14px 16px;
            border-radius: 14px;
            border: 1px solid var(--line);
            background: #fbfdff;
        }}
        .meta-label {{
            display: block;
            margin-bottom: 6px;
            color: var(--muted);
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }}
        .meta-value {{
            display: block;
            font-size: 14px;
            line-height: 1.5;
            word-break: break-word;
        }}
        .metrics {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 12px;
        }}
        .metric-card {{
            padding: 18px;
            border-radius: 16px;
            background: linear-gradient(180deg, #ffffff 0%, #f8fbff 100%);
            border: 1px solid var(--line);
        }}
        .metric-value {{
            font-size: 28px;
            font-weight: 700;
            color: var(--accent);
        }}
        .metric-label {{
            margin-top: 8px;
            color: var(--muted);
            font-size: 13px;
        }}
        .trend-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
            gap: 16px;
        }}
        .trend-card {{
            padding: 16px;
            border-radius: 16px;
            border: 1px solid var(--line);
            background: linear-gradient(180deg, #ffffff 0%, #f8fbff 100%);
        }}
        .trend-card h3 {{
            margin: 0 0 6px;
            font-size: 16px;
        }}
        .trend-unit {{
            margin-bottom: 10px;
            color: var(--muted);
            font-size: 12px;
        }}
        .trend-chart {{
            width: 100%;
            height: auto;
            background: #fbfdff;
            border-radius: 12px;
        }}
        .trend-grid-line {{
            stroke: #dbe3ee;
            stroke-dasharray: 4 4;
        }}
        .trend-axis-label {{
            fill: #5c6b83;
            font-size: 11px;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        }}
        .trend-legend {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin-top: 12px;
        }}
        .trend-legend-item {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            color: var(--muted);
            font-size: 12px;
        }}
        .trend-legend-dot {{
            width: 10px;
            height: 10px;
            border-radius: 999px;
            display: inline-block;
        }}
        .table-wrap {{
            overflow-x: auto;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
        }}
        th, td {{
            padding: 12px 10px;
            border-bottom: 1px solid var(--line);
            text-align: left;
            vertical-align: top;
        }}
        th {{
            color: var(--muted);
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            background: #f8fbff;
        }}
        .empty-state {{
            border: 1px dashed var(--line);
            border-radius: 14px;
            padding: 18px;
            color: var(--muted);
            background: #fbfdff;
        }}
        .error-list {{
            display: grid;
            gap: 12px;
        }}
        .error-item {{
            padding: 14px 16px;
            border-radius: 14px;
            border: 1px solid #fecaca;
            background: var(--danger-soft);
        }}
        .error-item strong {{
            display: block;
            margin-bottom: 6px;
            color: var(--danger);
        }}
        .ai-summary {{
            display: grid;
            gap: 12px;
        }}
        .ai-summary-box {{
            padding: 14px 16px;
            border-radius: 14px;
            border: 1px solid var(--line);
            background: #fbfdff;
        }}
        .ai-summary-box strong {{
            display: block;
            margin-bottom: 6px;
        }}
        .ai-summary-box span {{
            color: var(--muted);
            font-size: 13px;
        }}
        .ai-panel {{
            padding: 14px 16px;
            border-radius: 14px;
            border: 1px solid var(--line);
            background: #ffffff;
        }}
        .ai-panel h3 {{
            margin: 0 0 10px;
            font-size: 15px;
        }}
        .ai-list {{
            margin: 0;
            padding-left: 20px;
            color: var(--text);
        }}
        .ai-list li {{
            margin: 6px 0;
            line-height: 1.55;
        }}
        .ai-summary-text {{
            line-height: 1.6;
        }}
        .ai-disclaimer {{
            margin-top: 12px;
            color: var(--muted);
            font-size: 12px;
            line-height: 1.5;
        }}
        details {{
            border: 1px solid var(--line);
            border-radius: 14px;
            padding: 14px 16px;
            background: #fbfdff;
        }}
        details summary {{
            cursor: pointer;
            font-weight: 600;
        }}
        pre {{
            margin: 12px 0 0;
            padding: 16px;
            border-radius: 12px;
            background: #0f172a;
            color: #e2e8f0;
            overflow-x: auto;
            font-size: 12px;
            line-height: 1.5;
        }}
    </style>
</head>
<body data-report-template-version="{self._escape(REPORT_TEMPLATE_VERSION)}">
    <div class="page">
        <div class="hero">
            <h1>{self._escape(report.name)}</h1>
            <p>{self._escape(report.description or "性能测试结果下载报告")}</p>
            <p>生成时间：{self._escape(self._format_datetime(datetime.now(timezone.utc)))}</p>
            <p>报告时区：Asia/Shanghai（北京时间）</p>
        </div>

        <div class="section">
            <h2>基础元数据</h2>
            <div class="meta-grid">
                {"".join(metadata_items)}
            </div>
        </div>

        <div class="section">
            <h2>总体指标</h2>
            <p class="section-note">本区优先展示下载报告最需要快速扫读的总体性能指标。</p>
            <div class="metrics">
                {"".join(overall_cards)}
            </div>
        </div>

        <div class="section">
            <h2>接口级指标</h2>
            <p class="section-note">优先按接口维度展示请求量、吞吐量和关键分位延迟，便于离线分发和复盘。</p>
            {self._render_table(
                [
                    ("接口", "endpoint_name"),
                    ("总请求数", "total_requests"),
                    ("吞吐量", "throughput"),
                    ("平均 RT", "avg_rt_ms"),
                    ("P95", "p95_rt_ms"),
                    ("P99", "p99_rt_ms"),
                ],
                endpoint_rows,
            )}
        </div>

        <div class="section">
            <h2>趋势曲线</h2>
            <p class="section-note">统一 HTML 报告仍保持同一结构，同时补齐接口 TPS / avg / p95 / p99 曲线，便于 JMeter 与 K6 下载报告对齐阅读心智。</p>
            <div class="trend-grid">
                {"".join(trend_cards)}
            </div>
        </div>

        <div class="section">
            <h2>Checks / 错误概览</h2>
            <p class="section-note">先给出 checks 成功率，再集中列出失败请求或停止原因。</p>
            {self._render_table(
                [
                    ("Group", "group_name"),
                    ("Check", "check_name"),
                    ("成功率", "success_rate"),
                ],
                checks_rows,
            )}
            <div style="height: 16px;"></div>
            <div class="error-list">
                {"".join(
                    f'<div class="error-item"><strong>{self._escape(item["summary"])}</strong><span>{self._escape(item["detail"])}</span></div>'
                    for item in error_rows
                ) or '<div class="empty-state">当前未记录失败请求、停止原因或生成错误。</div>'}
            </div>
        </div>

        <div class="section">
            <h2>执行归档</h2>
            <div class="meta-grid">
                {"".join(artifact_items)}
            </div>
        </div>

        <div class="section">
            <h2>关联监控分析结论</h2>
            <p class="section-note">本区基于任务关联的业务大盘、MySQL、Redis、拓扑入口与 Prometheus 机器指标生成，不调用 AI。</p>
            <div class="ai-summary-box">
                {self._escape(observability_analysis.get("summary") or "暂无关联监控分析结论。")}
            </div>
            <div style="height: 16px;"></div>
            {self._render_table(
                [
                    ("对象", "title"),
                    ("状态", "status"),
                    ("结论", "conclusion"),
                    ("关键指标", "metrics"),
                ],
                observability_analysis_rows,
            )}
        </div>

        <div class="section">
            <h2>AI 根因假设</h2>
            <p class="section-note">本区仅串联最新 AI Report 元数据，下载报告语义仍以原始指标和证据为准。</p>
            {self._render_ai_report_section(ai_report_metadata)}
        </div>

            <div class="section">
                <h2>附录</h2>
                <details>
                    <summary>测试配置</summary>
                    <pre>{self._escape(json.dumps(report.test_config or {}, indent=2, ensure_ascii=False))}</pre>
                </details>
            <div style="height: 12px;"></div>
            <details>
                <summary>原始指标数据</summary>
                <pre>{self._escape(json.dumps(data, indent=2, ensure_ascii=False))}</pre>
            </details>
        </div>
    </div>
</body>
</html>
        """
        return html

    def mark_report_failed(self, report_id: int, error_message: str):
        """标记报告生成失败"""
        self.repo.update(
            report_id,
            {"status": ReportStatus.FAILED, "metrics_data": {"error": error_message}},
        )
