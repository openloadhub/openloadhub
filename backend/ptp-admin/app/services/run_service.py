from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from copy import deepcopy
import json
import os
import random
import re
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from common.config.settings import get_run_artifact_prefix, settings
from common.utils import s3_utils
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models.run import Run
from app.models.run_baseline import RunBaseline
from app.models.script import Script, ScriptType
from app.models.task import Task
from app.models.user import User, UserRole
from app.repositories.run_repository import RunRepository
from app.services.alert_event_service import AlertEventService
from app.services.observability_query_service import (
    ObservabilityQueryResult,
    ObservabilityQueryService,
)
from app.services.run_analysis_readiness_service import RunAnalysisReadinessService
from app.services.self_apm_service import SelfApmService
from app.services.task_asset_service import TaskAssetService
from common.models.enums import (
    EngineType,
    RunBaselineScopeType,
    RunBaselineSource,
    RunStatus,
)
from common.schemas.run import (
    LogItem,
    LogsResponse,
    MetricName,
    MetricPoint,
    MetricsResponse,
    MetricsSeries,
    RunCreate,
    RunSummaryMetricRow,
    RunSummaryMetricsResponse,
    RunCheckRow,
    RunChecksResponse,
    RunPodStatus,
    RunPodStatusResponse,
    RunPodMonitorMetric,
    RunPodMonitorSeries,
    RunPodMonitorResponse,
    RunPodMonitorSummary,
    RunDashboardType,
    RunDashboardLink,
    RunDashboardsResponse,
    EndpointTrendMetric,
    EndpointTrendSeries,
    EndpointTrendResponse,
    RunOverviewSummary,
    RunDashboardSummary,
    RunK6ControlRequest,
    RunK6ScenarioConfig,
    RunK6ControlAgentState,
    RunK6ControlSummary,
    RunK6ControlResponse,
    RunK6ControlAcceptedResponse,
    RunK6ControlTaskStatusResponse,
    RunBaselineResponse,
    RunBaselineRunSummary,
    RunBaselineSetRequest,
    RunVerdictMetricDelta,
    RunVerdictResponse,
    RunAIInsightItem,
    RunAIEvidenceItem,
    RunAIPrimaryFocus,
    RunAIRootCauseHypothesis,
    RunAIAnalystResponse,
)
from common.schemas.run import RunCompareBundle, RunCompareResponse

logger = logging.getLogger(__name__)
_K6_GRPC_INVOKE_PATTERN = re.compile(
    r"\b(?:[\w$.]+\s*\.)?(?:invoke|asyncInvoke)\(\s*['\"]([^'\"]+/[^'\"]+)['\"]"
)
_SCENARIO_DIRECT_RUNTIME_NOT_APPLIED_DETAIL = "scenario_direct_runtime_not_applied"


@dataclass
class BulkStopActiveRunsResult:
    stopped_runs: list[Run]
    remote_stop_summary: dict[str, Any] = field(default_factory=dict)


def _resolve_protocol_snapshot(protocols: Any) -> Optional[str]:
    if not isinstance(protocols, list):
        return None
    normalized: list[str] = []
    for item in protocols:
        if isinstance(item, str) and item.strip():
            value = item.strip().lower()
            if value not in normalized:
                normalized.append(value)
    if not normalized:
        return None
    return "mixed" if len(normalized) > 1 else normalized[0]


class RunService:
    IDEMPOTENCY_WINDOW_SECONDS = 60
    TERMINAL_POD_STATUSES = {
        "succeeded",
        "completed",
        "failed",
        "stopped",
        "terminated",
    }
    TERMINAL_RUN_STATUSES = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.STOPPED}
    DASHBOARD_WINDOW_START_PADDING_MS = 10_000
    DASHBOARD_WINDOW_END_PADDING_MS = 30_000
    DASHBOARD_LIVE_RELATIVE_FROM = "now-5m"
    DASHBOARD_LIVE_RELATIVE_TO = "now"
    POD_GRAFANA_DEFAULT_COMPOSE_SERVICE_REGEX = ".*"
    TARGET_SERVICE_DASHBOARD_PATH = "/d/demo-target-dashboard/demo-target-service-dashboard"
    DEMO_TARGET_DASHBOARD_PATH = "/d/demo-target-dashboard/demo-target-service-dashboard"
    _TERMINAL_SYNC_LOCKS: dict[int, threading.Lock] = {}
    _TERMINAL_SYNC_LOCKS_GUARD = threading.Lock()
    EXCEPTION_LOG_SCAN_LIMIT_PER_SOURCE = 2000
    EXCEPTION_LOG_SCAN_CHUNK_SIZE = 500
    GRAFANA_DASHBOARD_SLUGS = {
        "k6-prometheus-ptp": "k6-prometheus-dashboard-ptp",
        "k6-grpc-ptp": "k6-grpc-dashboard-ptp",
        "21Ev3D0Ik": "k6-websocket-dashboard",
        "usA2Xd_4z": "k6-kafka-dashboard",
        "j9zA7u9Ik": "k6-browser-dashboard",
        "jmeter-load-test-influx": "jmeter-load-test-influx",
        "pod-monitor-dashboard": "pod-agent-monitor-dashboard",
        "pod-monitor-dashboard-host": "pod-agent-monitor-dashboard-host",
    }

    def __init__(self, db: Session):
        self.db = db
        self.repo = RunRepository(db)
        self._agent_json_cache: dict[tuple[str, str, str], Any] = {}

    def create_run(
        self, run_in: RunCreate, user_id: int, idempotency_key: Optional[str] = None
    ) -> Run:
        if idempotency_key:
            existing = self.repo.find_by_idempotency_key_recent(
                idempotency_key, self.IDEMPOTENCY_WINDOW_SECONDS
            )
            if existing:
                return existing

        task = self.db.query(Task).filter(Task.id == run_in.task_id).first()
        if not task:
            raise ValueError("Task not found")
        if (
            task.created_by
            and int(task.created_by) != int(user_id)
            and not self._is_task_access_exempt_user(user_id)
        ):
            raise PermissionError("Forbidden: owner only")

        now_ts = datetime.now(timezone.utc)
        params = dict(run_in.params or {})
        if os.environ.get("TESTING", "0") != "1":
            self._validate_requested_pod_capacity(task, params)
        task_properties = task.properties if isinstance(task.properties, dict) else {}
        self._inject_task_runtime_params_snapshot(params, task_properties)
        task_asset_service = TaskAssetService(self.db)
        task_asset_service.inject_runtime_asset_file_defaults(task, params)
        task_asset_service.validate_runtime_asset_bindings(task, params)
        self._inject_observability_snapshot(params, task_properties)
        raw_business_line = task_properties.get("business_line")
        if (
            isinstance(raw_business_line, str)
            and raw_business_line.strip()
            and not isinstance(params.get("business_line"), str)
        ):
            params["business_line"] = raw_business_line.strip()
        seeded_status = self._parse_seed_run_status(params.get("seed_run_status"))
        seeded_started_at = self._parse_ts(params.get("seed_started_at"))
        seeded_ended_at = self._parse_ts(params.get("seed_ended_at"))
        seeded_duration = self._parse_seed_duration_seconds(
            params.get("seed_duration_seconds")
        )
        seeded_total_requests = self._parse_seed_int(params.get("seed_total_requests"))
        seeded_success_rate = self._parse_seed_ratio(params.get("seed_success_rate"))
        seeded_error_rate = self._parse_seed_ratio(params.get("seed_error_rate"))
        seeded_avg_rt_ms = self._parse_seed_float(params.get("seed_avg_rt_ms"))
        seeded_p95_rt_ms = self._parse_seed_float(params.get("seed_p95_rt_ms"))
        seeded_p99_rt_ms = self._parse_seed_float(params.get("seed_p99_rt_ms"))
        seeded_rps = self._parse_seed_float(params.get("seed_rps"))
        if seeded_success_rate is None and seeded_error_rate is not None:
            seeded_success_rate = max(0.0, min(1.0, 1 - seeded_error_rate))
        if seeded_error_rate is None and seeded_success_rate is not None:
            seeded_error_rate = max(0.0, min(1.0, 1 - seeded_success_rate))
        resolved_started_at = seeded_started_at or now_ts
        resolved_ended_at = seeded_ended_at
        resolved_status = seeded_status or RunStatus.PREPARING
        resolved_status_detail = (
            params.get("seed_run_status_detail")
            if isinstance(params.get("seed_run_status_detail"), str)
            else ("preparing_init" if resolved_status == RunStatus.PREPARING else None)
        )
        resolved_duration = seeded_duration
        if resolved_duration is None and resolved_started_at and resolved_ended_at:
            resolved_duration = max(
                0, int((resolved_ended_at - resolved_started_at).total_seconds())
            )

        run = Run(
            task_id=task.id,
            task_name=task.name,
            engine_type=task.engine_type,
            env=task.env,
            protocol=_resolve_protocol_snapshot(task.protocols),
            run_status=resolved_status,
            run_status_detail=resolved_status_detail,
            started_at=resolved_started_at,
            ended_at=resolved_ended_at,
            duration_seconds=resolved_duration,
            params=params,
            total_requests=seeded_total_requests,
            success_rate=seeded_success_rate,
            error_rate=seeded_error_rate,
            avg_rt_ms=seeded_avg_rt_ms,
            p95_rt_ms=seeded_p95_rt_ms,
            p99_rt_ms=seeded_p99_rt_ms,
            rps=seeded_rps,
            idempotency_key=idempotency_key,
        )

        try:
            created = self.repo.create(run)
        except IntegrityError:
            if not idempotency_key:
                raise
            self.db.rollback()
            existing = self.repo.find_by_idempotency_key(idempotency_key)
            if existing:
                return existing
            raise

        task.last_run_at = resolved_started_at or now_ts
        self.db.commit()
        return created

    @classmethod
    def _inject_task_runtime_params_snapshot(
        cls,
        params: dict[str, Any],
        task_properties: dict[str, Any],
    ) -> None:
        """Persist the task runtime context that is needed to explain a run later."""

        if not task_properties:
            return

        if not isinstance(params.get("properties"), dict):
            params["properties"] = deepcopy(task_properties)

        raw_variables = task_properties.get("variables")
        if isinstance(raw_variables, dict):
            if not isinstance(params.get("variables"), dict):
                params["variables"] = deepcopy(raw_variables)
            for key, value in raw_variables.items():
                if (
                    isinstance(key, str)
                    and key.strip()
                    and key not in params
                    and cls._is_runtime_snapshot_scalar(value)
                ):
                    params[key] = deepcopy(value)

        for key in (
            "BASE_URL",
            "GRPC_HOST",
            "GRPC_PORT",
            "DATA_FILE",
            "DATA_DELIMITER",
            "target_host",
            "target_port",
            "target_protocol",
            "target_get_path",
            "target_post_path",
            "target_tps",
            "fixed_tps",
            "target_tpm",
            "duration",
            "thread_count",
            "vus",
            "pod_count",
            "pod_num",
            "run_by",
            "scheduler_enabled",
            "metrics_enabled",
            "data_distribution",
            "resource_type",
            "cloud_vendor",
        ):
            if key in params:
                continue
            value = task_properties.get(key)
            if cls._is_runtime_snapshot_scalar(value):
                params[key] = deepcopy(value)

    @staticmethod
    def _is_runtime_snapshot_scalar(value: Any) -> bool:
        return isinstance(value, (str, int, float, bool))

    def _validate_requested_pod_capacity(
        self, task: Task, params: dict[str, Any]
    ) -> None:
        requested = self._resolve_requested_pod_count(task, params)
        from app.services.task_service import TaskService

        pool = TaskService(self.db)._build_resource_pool(task.env)
        idle_total = max(0, int(getattr(pool, "idle_total", 0) or 0))
        if requested > idle_total:
            raise ValueError(
                f"执行节点不足：当前空闲 {idle_total} 个，申请 {requested} 个"
            )

    @staticmethod
    def _resolve_requested_pod_count(task: Task, params: dict[str, Any]) -> int:
        for key in ("pod_count", "pod_num", "pod_total"):
            value = params.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
            if isinstance(value, str):
                try:
                    parsed = int(float(value))
                except ValueError:
                    parsed = 0
                if parsed > 0:
                    return parsed

        properties = task.properties if isinstance(task.properties, dict) else {}
        raw = properties.get("pod_count") or properties.get("pod_num")
        if isinstance(raw, (int, float)) and raw > 0:
            return int(raw)
        return 1

    def _parse_seed_run_status(self, status_raw) -> Optional[RunStatus]:
        if isinstance(status_raw, RunStatus):
            return status_raw
        if isinstance(status_raw, str):
            try:
                return RunStatus(status_raw)
            except ValueError:
                return None
        return None

    def _parse_seed_duration_seconds(self, value) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return max(0, int(value))
        if isinstance(value, str):
            try:
                return max(0, int(float(value)))
            except ValueError:
                return None
        return None

    def _parse_seed_int(self, value) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            try:
                return int(float(value))
            except ValueError:
                return None
        return None

    def _parse_seed_float(self, value) -> Optional[float]:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        return None

    def _parse_seed_ratio(self, value) -> Optional[float]:
        parsed = self._parse_seed_float(value)
        if parsed is None:
            return None
        return max(0.0, min(1.0, parsed))

    def _pick_first_seed_int(self, *values) -> Optional[int]:
        for value in values:
            parsed = self._parse_seed_int(value)
            if parsed is not None:
                return parsed
        return None

    def _has_terminal_single_agent_fallback(self, run: Run, params: dict) -> bool:
        host = params.get("agent_host")
        token = (
            params.get("agent_run_token")
            or params.get("agent_token")
            or params.get("agent_session")
        )
        return (
            isinstance(host, str)
            and bool(host.strip())
            and isinstance(token, str)
            and bool(token.strip())
            and self._is_terminal_run_status(run.run_status)
        )

    def _build_terminal_single_agent_pod_status(
        self, run: Run, params: dict
    ) -> Optional[RunPodStatus]:
        if not self._has_terminal_single_agent_fallback(run, params):
            return None

        host = params.get("agent_host")
        if not isinstance(host, str) or not host.strip():
            return None

        host = host.strip()
        agent_ip = params.get("agent_ip")
        if not isinstance(agent_ip, str) or not agent_ip.strip():
            agent_ip = host.split(":", 1)[0]

        pod_name = (
            params.get("pod_name")
            or params.get("agent_name")
            or params.get("agent_pod_name")
        )
        if not isinstance(pod_name, str) or not pod_name.strip():
            pod_name = host.split(":", 1)[0]

        status = (
            run.run_status.value
            if isinstance(run.run_status, RunStatus)
            else str(run.run_status or "unknown")
        )
        return RunPodStatus(
            pod_ip=(
                agent_ip.strip()
                if isinstance(agent_ip, str) and agent_ip.strip()
                else None
            ),
            pod_name=(
                pod_name.strip()
                if isinstance(pod_name, str) and pod_name.strip()
                else None
            ),
            status=status,
            cluster_name=None,
            node_name=None,
            started_at=self._as_utc(run.started_at),
            ended_at=self._as_utc(run.ended_at),
        )

    def _is_terminal_run_status(self, status: RunStatus | str | None) -> bool:
        if isinstance(status, RunStatus):
            return status in self.TERMINAL_RUN_STATUSES
        if isinstance(status, str):
            try:
                return RunStatus(status) in self.TERMINAL_RUN_STATUSES
            except ValueError:
                return status.lower() in {"succeeded", "failed", "stopped"}
        return False

    def _build_run_overview_summary(self, run: Run, params: dict) -> RunOverviewSummary:
        summary_rows = self._extract_summary_metric_rows(params)
        check_rows = (
            params.get("checks") if isinstance(params.get("checks"), list) else []
        )
        k6_summary = (
            params.get("k6_summary")
            if isinstance(params.get("k6_summary"), dict)
            else {}
        )

        total_requests_values = []
        throughput_values = []
        avg_rt_values = []
        p95_values = []
        for row in summary_rows:
            if not isinstance(row, dict):
                continue
            total_requests = self._parse_seed_int(row.get("total_requests"))
            throughput = self._parse_seed_float(row.get("throughput"))
            avg_rt = self._parse_seed_float(row.get("avg_rt_ms"))
            p95_rt = self._parse_seed_float(row.get("p95_rt_ms"))
            if total_requests is not None:
                total_requests_values.append(total_requests)
            if throughput is not None:
                throughput_values.append(throughput)
            if avg_rt is not None:
                avg_rt_values.append(avg_rt)
            if p95_rt is not None:
                p95_values.append(p95_rt)

        check_rates = []
        for row in check_rows:
            if not isinstance(row, dict):
                continue
            success_rate = self._parse_seed_ratio(row.get("success_rate"))
            if success_rate is not None:
                check_rates.append(success_rate)

        error_rate = run.error_rate
        if error_rate is None and run.success_rate is not None:
            error_rate = max(0.0, min(1.0, 1 - run.success_rate))

        total_requests = (
            sum(total_requests_values)
            if total_requests_values
            else (
                run.total_requests
                or self._build_k6_iteration_fallback_total_requests(run, k6_summary)
            )
        )
        contract_throughput = self._resolve_terminal_k6_summary_throughput(run, params)
        throughput = (
            contract_throughput
            if contract_throughput is not None
            else (
                sum(throughput_values)
                if throughput_values
                else (run.rps or self._parse_seed_float(k6_summary.get("throughput")))
            )
        )
        avg_rt_ms = (
            (sum(avg_rt_values) / len(avg_rt_values))
            if avg_rt_values
            else (run.avg_rt_ms or self._parse_seed_float(k6_summary.get("rt_avg_ms")))
        )
        p95_rt_ms = (
            max(p95_values)
            if p95_values
            else (run.p95_rt_ms or self._parse_seed_float(k6_summary.get("rt_p95_ms")))
        )
        if check_rates:
            checks_success_rate = sum(check_rates) / len(check_rates)
        else:
            checks_success_rate = self._derive_k6_checks_success_rate(k6_summary)
            if checks_success_rate is None:
                checks_success_rate = run.success_rate
        endpoint_total = len(summary_rows) if summary_rows else None
        check_total = len(check_rows) if check_rows else None

        return RunOverviewSummary(
            total_requests=total_requests,
            throughput=throughput,
            avg_rt_ms=avg_rt_ms,
            p95_rt_ms=p95_rt_ms,
            error_rate=error_rate,
            checks_success_rate=checks_success_rate,
            endpoint_total=endpoint_total,
            summary_metrics_label=self._format_summary_metrics_label(
                endpoint_total, total_requests, throughput
            ),
            checks_summary_label=self._format_checks_summary_label(
                checks_success_rate, check_total
            ),
        )

    @staticmethod
    def _extract_summary_metric_rows(params: dict[str, Any]) -> list[dict[str, Any]]:
        seed_rows = params.get("summary_metrics")
        if isinstance(seed_rows, list):
            return [row for row in seed_rows if isinstance(row, dict)]

        k6_summary = params.get("k6_summary")
        endpoint_rows = (
            k6_summary.get("endpoint_metrics") if isinstance(k6_summary, dict) else None
        )
        if isinstance(endpoint_rows, list):
            return [row for row in endpoint_rows if isinstance(row, dict)]
        return []

    def _resolve_terminal_k6_summary_throughput(
        self,
        run: Run,
        params: dict[str, Any],
        *,
        contract_throughput: Optional[float] = None,
    ) -> Optional[float]:
        if not self._is_k6_engine(run) or not self._is_terminal_run_status(
            run.run_status
        ):
            return None
        k6_summary = (
            params.get("k6_summary")
            if isinstance(params.get("k6_summary"), dict)
            else {}
        )
        summary_throughput = self._parse_seed_float(k6_summary.get("throughput"))
        if summary_throughput is None:
            return None
        if self._is_k6_mixed_run(run):
            return summary_throughput

        if contract_throughput is None and self._has_real_metric_context(run):
            _, contract_throughput = self._resolve_k6_overview_contract_totals(run)
        if contract_throughput is None or contract_throughput <= 0:
            return summary_throughput

        delta_ratio = abs(float(summary_throughput) - float(contract_throughput)) / max(
            abs(float(contract_throughput)),
            1.0,
        )
        if delta_ratio > 0.1:
            return None
        return summary_throughput

    @staticmethod
    def _extract_endpoint_trend_seed_items(
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        seed_trends = params.get("endpoint_trends")
        if isinstance(seed_trends, list):
            return [item for item in seed_trends if isinstance(item, dict)]

        k6_summary = params.get("k6_summary")
        endpoint_trends = (
            k6_summary.get("endpoint_trends") if isinstance(k6_summary, dict) else None
        )
        if isinstance(endpoint_trends, list):
            return [item for item in endpoint_trends if isinstance(item, dict)]
        return []

    @staticmethod
    def _extract_check_rows(params: dict[str, Any]) -> list[dict[str, Any]]:
        seed_checks = params.get("checks")
        if isinstance(seed_checks, list):
            return [row for row in seed_checks if isinstance(row, dict)]

        k6_summary = params.get("k6_summary")
        summary_checks = (
            k6_summary.get("checks") if isinstance(k6_summary, dict) else None
        )
        if isinstance(summary_checks, list):
            return [row for row in summary_checks if isinstance(row, dict)]
        return []

    def _derive_k6_success_rate_from_error_rate(
        self, k6_summary: dict
    ) -> Optional[float]:
        error_rate = self._parse_seed_ratio(k6_summary.get("error_rate"))
        if error_rate is None:
            return None
        return max(0.0, min(1.0, 1 - error_rate))

    def _derive_k6_checks_success_rate(self, k6_summary: dict) -> Optional[float]:
        checks_rate = self._parse_seed_ratio(k6_summary.get("checks_rate"))
        if checks_rate is not None:
            return checks_rate
        success_rate = self._parse_seed_ratio(k6_summary.get("success_rate"))
        if success_rate is not None:
            return success_rate
        return self._derive_k6_success_rate_from_error_rate(k6_summary)

    def _build_k6_iteration_fallback_total_requests(
        self, run: Run, k6_summary: Optional[dict[str, object]] = None
    ) -> Optional[int]:
        if run.engine_type != EngineType.K6:
            return None
        summary = (
            k6_summary
            if isinstance(k6_summary, dict)
            else (
                (run.params or {}).get("k6_summary")
                if isinstance((run.params or {}).get("k6_summary"), dict)
                else {}
            )
        )
        metric_family = str(summary.get("metric_family") or "").strip().lower()
        if metric_family not in {"grpc", "iteration"}:
            return None
        return self._parse_seed_int(
            summary.get("total_requests")
        ) or self._parse_seed_int(summary.get("iterations"))

    def _is_k6_grpc_or_iteration_run(self, run: Run) -> bool:
        if not self._is_k6_engine(run):
            return False
        protocol = self._resolve_run_protocol(run)
        summary = (run.params or {}).get("k6_summary")
        metric_family = (
            str(summary.get("metric_family") or "").strip().lower()
            if isinstance(summary, dict)
            else ""
        )
        return protocol == "grpc" or metric_family in {"grpc", "iteration"}

    def _is_k6_mixed_run(self, run: Run) -> bool:
        if not self._is_k6_engine(run):
            return False
        protocol = self._resolve_run_protocol(run)
        if protocol == "mixed":
            return True

        if self.db is not None and run.task_id:
            task = self.db.query(Task).filter(Task.id == run.task_id).first()
            task_protocols = task.protocols if task else None
            if isinstance(task_protocols, list):
                normalized = {
                    item.strip().lower()
                    for item in task_protocols
                    if isinstance(item, str) and item.strip()
                }
                if {"http", "grpc"}.issubset(normalized):
                    return True

        params = run.params or {}
        checks = params.get("checks")
        if isinstance(checks, list):
            has_http_checks = any(
                isinstance(item, dict)
                and str(item.get("check_name") or "").strip().startswith("HTTP ")
                for item in checks
            )
            has_grpc_checks = any(
                isinstance(item, dict)
                and str(item.get("check_name") or "").strip().startswith("GRPC ")
                for item in checks
            )
            if has_http_checks and has_grpc_checks:
                return True

        summary_rows = self._extract_summary_metric_rows(params)
        if summary_rows:
            has_http_rows = any(
                isinstance(item, dict)
                and str(item.get("endpoint_name") or "").startswith(
                    ("GET ", "POST ", "PUT ", "DELETE ", "PATCH ")
                )
                for item in summary_rows
            )
            has_grpc_rows = any(
                isinstance(item, dict)
                and str(item.get("endpoint_name") or "").startswith("hello.")
                for item in summary_rows
            )
            if has_http_rows and has_grpc_rows:
                return True
        return False

    def _resolve_run_protocol(self, run: Run) -> str:
        protocol = str(run.protocol or "").strip().lower()
        if protocol:
            return protocol

        params = run.params or {}
        param_protocol = params.get("protocol")
        if isinstance(param_protocol, str) and param_protocol.strip():
            return param_protocol.strip().lower()

        if self.db is not None and run.task_id:
            task = self.db.query(Task).filter(Task.id == run.task_id).first()
            task_protocols = task.protocols if task else None
            resolved = _resolve_protocol_snapshot(task_protocols)
            if resolved:
                return resolved

        summary = params.get("k6_summary")
        metric_family = (
            str(summary.get("metric_family") or "").strip().lower()
            if isinstance(summary, dict)
            else ""
        )
        metric_family_protocol_map = {
            "browser": "browser",
            "grpc": "grpc",
            "http": "http",
            "iteration": "grpc",
            "mixed": "mixed",
        }
        return metric_family_protocol_map.get(metric_family, "")

    def _resolve_run_protocols(self, run: Run) -> set[str]:
        protocols: set[str] = set()

        direct_protocol = str(run.protocol or "").strip().lower()
        if direct_protocol:
            protocols.add(direct_protocol)

        params = run.params or {}
        param_protocol = params.get("protocol")
        if isinstance(param_protocol, str) and param_protocol.strip():
            protocols.add(param_protocol.strip().lower())
        param_protocols = params.get("protocols")
        if isinstance(param_protocols, list):
            protocols.update(
                item.strip().lower()
                for item in param_protocols
                if isinstance(item, str) and item.strip()
            )

        current_task_protocols = getattr(run, "current_task_protocols", None)
        if isinstance(current_task_protocols, list):
            protocols.update(
                item.strip().lower()
                for item in current_task_protocols
                if isinstance(item, str) and item.strip()
            )

        metric_family = (
            str(params.get("k6_summary", {}).get("metric_family") or "").strip().lower()
            if isinstance(params.get("k6_summary"), dict)
            else ""
        )
        metric_family_protocol_map = {
            "browser": "browser",
            "grpc": "grpc",
            "http": "http",
            "iteration": "grpc",
        }
        mapped_protocol = metric_family_protocol_map.get(metric_family)
        if mapped_protocol:
            protocols.add(mapped_protocol)

        return protocols

    def _build_user_display_map(self, user_ids: set[int]) -> dict[int, str]:
        if not user_ids:
            return {}
        rows = self.db.query(User).filter(User.id.in_(user_ids)).all()
        mapping: dict[int, str] = {}
        for row in rows:
            label = row.full_name or row.username
            if label:
                normalized = str(label).strip()
                if normalized.casefold() == "administrator":
                    normalized = "admin"
                mapping[int(row.id)] = normalized
        return mapping

    @staticmethod
    def _normalize_multi_value_filter(raw_value: Optional[str]) -> set[str]:
        normalized_raw = str(getattr(raw_value, "value", raw_value) or "").strip()
        if not normalized_raw:
            return set()
        return {
            item.strip().casefold()
            for item in normalized_raw.split(",")
            if item.strip()
        }

    def _matches_run_protocol_filter(
        self, run: Run, protocol_filter: Optional[str]
    ) -> bool:
        expected = self._normalize_multi_value_filter(protocol_filter)
        if not expected:
            return True
        actual = {
            item.strip().casefold()
            for item in self._resolve_run_protocols(run)
            if isinstance(item, str) and item.strip()
        }
        return bool(actual & expected)

    def _matches_run_engine_type_filter(
        self, run: Run, engine_type_filter: Optional[str]
    ) -> bool:
        expected = self._normalize_multi_value_filter(engine_type_filter)
        if not expected:
            return True
        actual = (
            str(
                getattr(
                    getattr(run, "engine_type", None),
                    "value",
                    getattr(run, "engine_type", None),
                )
                or ""
            )
            .strip()
            .casefold()
        )
        return actual in expected

    def _matches_run_business_line_filter(
        self, run: Run, business_line_filter: Optional[str]
    ) -> bool:
        expected = self._normalize_multi_value_filter(business_line_filter)
        if not expected:
            return True
        actual_candidates = {
            value.strip().casefold()
            for value in (
                getattr(run, "current_task_business_line", None),
                getattr(run, "business_line", None),
            )
            if isinstance(value, str) and value.strip()
        }
        return bool(actual_candidates & expected)

    def _matches_run_task_pattern_filter(
        self, run: Run, task_pattern_filter: Optional[str]
    ) -> bool:
        expected = self._normalize_multi_value_filter(task_pattern_filter)
        if not expected:
            return True
        actual = (
            str(getattr(run, "current_task_pattern", None) or "").strip().casefold()
        )
        return actual in expected

    @staticmethod
    def _normalize_endpoint_name(value: Any) -> str:
        endpoint_name = str(value or "").strip()
        if not endpoint_name:
            return ""
        if endpoint_name.startswith("/") and "." in endpoint_name[1:].split("/", 1)[0]:
            return endpoint_name[1:]
        return endpoint_name

    def _attach_run_display_fields(
        self,
        runs: list[Run],
        *,
        include_live_runtime_enrichment: bool = True,
    ) -> None:
        if not runs:
            return

        task_ids = {int(run.task_id) for run in runs if getattr(run, "task_id", None)}
        tasks = (
            self.db.query(Task).filter(Task.id.in_(task_ids)).all() if task_ids else []
        )
        task_map = {int(task.id): task for task in tasks}
        user_ids = {
            int(task.created_by) for task in tasks if getattr(task, "created_by", None)
        }
        user_map = self._build_user_display_map(user_ids)

        for run in runs:
            task = (
                task_map.get(int(run.task_id))
                if getattr(run, "task_id", None)
                else None
            )
            params = run.params or {}
            if include_live_runtime_enrichment:
                enriched_k6_summary = self._hydrate_live_k6_summary_from_agent_status(
                    run, params
                )
                if enriched_k6_summary is not None:
                    params = dict(params)
                    params["k6_summary"] = enriched_k6_summary
                    run.params = params
            properties = (
                task.properties if task and isinstance(task.properties, dict) else {}
            )
            run_properties = (
                params.get("properties")
                if isinstance(params.get("properties"), dict)
                else {}
            )

            run_business_line = params.get("business_line")
            if isinstance(run_business_line, str) and run_business_line.strip():
                run.business_line = run_business_line.strip()
            else:
                raw_business_line = properties.get("business_line")
                run.business_line = (
                    raw_business_line.strip()
                    if isinstance(raw_business_line, str) and raw_business_line.strip()
                    else None
                )
            run.current_task_env = task.env if task else None
            current_task_business_line = properties.get("business_line")
            run.current_task_business_line = (
                current_task_business_line.strip()
                if isinstance(current_task_business_line, str)
                and current_task_business_line.strip()
                else None
            )
            current_task_pattern = getattr(task, "task_pattern", None)
            run.current_task_pattern = (
                getattr(current_task_pattern, "value", current_task_pattern)
                if current_task_pattern is not None
                else None
            )
            task_protocols = getattr(task, "protocols", None)
            run.current_task_protocols = (
                [
                    item.strip()
                    for item in task_protocols
                    if isinstance(item, str) and item.strip()
                ]
                if isinstance(task_protocols, list)
                else None
            )

            operator_name = (
                params.get("operator_name")
                if isinstance(params.get("operator_name"), str)
                else None
            )
            if task and task.created_by:
                creator_id = int(task.created_by)
                operator_name = user_map.get(creator_id, f"user#{creator_id}")
            run.operator_name = operator_name

            pod_total = self._pick_first_seed_int(
                params.get("pod_count"),
                params.get("pod_num"),
                params.get("pod_total"),
                run_properties.get("pod_count"),
                run_properties.get("pod_num"),
                run_properties.get("pod_total"),
                properties.get("pod_count"),
                properties.get("pod_num"),
                properties.get("pod_total"),
            )

            pod_rows = params.get("k8s_pods")
            if not isinstance(pod_rows, list):
                pod_rows = params.get("pods")
            if not isinstance(pod_rows, list):
                pod_rows = []
            observed_pod_total = self._infer_observed_pod_total_from_monitor_seed(
                params
            )

            agent_contexts = self._get_agent_contexts(run)
            has_agent_execution_context = bool(agent_contexts)
            if (
                include_live_runtime_enrichment
                and run.engine_type == EngineType.JMETER
                and has_agent_execution_context
            ):
                live_summary_metrics = (
                    self._fetch_agent_summary_metrics_from_all_contexts(
                        run.run_id,
                        agent_contexts,
                    )
                )
                if live_summary_metrics and live_summary_metrics.items:
                    params = dict(params)
                    params["summary_metrics"] = [
                        item.model_dump() for item in live_summary_metrics.items
                    ]
                    run.params = params

            if (
                pod_total is None
                and has_agent_execution_context
                and run.run_status != RunStatus.PREPARING
            ):
                pod_total = max(1, len(agent_contexts))

            pod_actual = (
                len(pod_rows)
                if pod_rows
                else self._parse_seed_int(params.get("pod_actual"))
            )
            if pod_actual is None and observed_pod_total is not None:
                pod_actual = observed_pod_total
            pod_completed = self._parse_seed_int(params.get("pod_completed"))
            if pod_completed is None and pod_rows:
                pod_completed = sum(
                    1
                    for row in pod_rows
                    if isinstance(row, dict)
                    and str(row.get("status", "")).strip().lower()
                    in self.TERMINAL_POD_STATUSES
                )
            agent_run_rows = [
                item for item in params.get("agent_runs", []) if isinstance(item, dict)
            ]
            if agent_run_rows and run.run_status != RunStatus.PREPARING:
                pod_actual = max(pod_actual or 0, len(agent_run_rows))
                if pod_completed is None:
                    pod_completed = sum(
                        1
                        for item in agent_run_rows
                        if str(item.get("status", "")).strip().lower()
                        in self.TERMINAL_POD_STATUSES
                    )
            if (
                pod_completed is None
                and observed_pod_total is not None
                and self._is_terminal_run_status(run.run_status)
            ):
                pod_completed = observed_pod_total
            if (
                pod_total is None
                and not pod_rows
                and self._has_terminal_single_agent_fallback(run, params)
            ):
                pod_total = 1
            if (
                pod_actual is None
                and pod_total is not None
                and run.run_status != RunStatus.PREPARING
            ):
                pod_actual = pod_total
            if (
                pod_completed is None
                and pod_total is not None
                and self._is_terminal_run_status(run.run_status)
            ):
                pod_completed = pod_total
            if pod_total is None:
                pod_total = pod_actual

            run.pod_total = pod_total
            run.pod_actual = pod_actual
            run.pod_completed = pod_completed
            if include_live_runtime_enrichment:
                contract_total_requests, contract_throughput = (
                    self._resolve_k6_overview_contract_totals(run)
                )
                terminal_k6_throughput = self._resolve_terminal_k6_summary_throughput(
                    run,
                    params,
                    contract_throughput=contract_throughput,
                )
                if contract_total_requests is not None:
                    run.total_requests = contract_total_requests
                if terminal_k6_throughput is not None:
                    run.rps = terminal_k6_throughput
                elif contract_throughput is not None:
                    run.rps = contract_throughput
            run.engine_type_label = self._label_engine_type(run.engine_type)
            run.run_status_label = self._label_run_status(run.run_status)
            run.run_window_label = self._format_run_window_label(
                run.started_at, run.ended_at
            )
            run.agent_progress_label = self._format_agent_progress_label(
                pod_completed, pod_actual, pod_total
            )
            run.overview_summary = self._build_run_overview_summary(run, params)

    def _hydrate_live_k6_summary_from_agent_status(
        self,
        run: Run,
        params: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        k6_summary = params.get("k6_summary")
        if not isinstance(k6_summary, dict):
            return None
        if run.engine_type != EngineType.K6 or not self._is_k6_grpc_or_iteration_run(
            run
        ):
            return k6_summary
        if (
            k6_summary.get("rt_max_ms") is not None
            and k6_summary.get("rt_min_ms") is not None
        ):
            return k6_summary

        max_candidates: list[float] = []
        min_candidates: list[float] = []
        metric_family = str(k6_summary.get("metric_family") or "").strip()

        for agent_ctx in self._get_agent_contexts(run):
            try:
                status_payload = self._fetch_agent_status(agent_ctx)
            except Exception as exc:
                logger.warning(
                    "hydrate_live_k6_summary_from_agent_status failed for run %s ctx=%s: %s",
                    run.run_id,
                    agent_ctx,
                    exc,
                )
                continue
            if not isinstance(status_payload, dict):
                continue
            agent_summary = status_payload.get("k6_summary")
            if not isinstance(agent_summary, dict):
                continue
            if not metric_family:
                metric_family = str(agent_summary.get("metric_family") or "").strip()
            max_value = self._parse_seed_float(agent_summary.get("rt_max_ms"))
            if max_value is not None:
                max_candidates.append(max_value)
            min_value = self._parse_seed_float(agent_summary.get("rt_min_ms"))
            if min_value is not None:
                min_candidates.append(min_value)

        if not max_candidates and not min_candidates and not metric_family:
            return k6_summary

        hydrated = dict(k6_summary)
        if metric_family and not str(hydrated.get("metric_family") or "").strip():
            hydrated["metric_family"] = metric_family
        if hydrated.get("rt_max_ms") is None and max_candidates:
            hydrated["rt_max_ms"] = max(max_candidates)
        if hydrated.get("rt_min_ms") is None and min_candidates:
            hydrated["rt_min_ms"] = min(min_candidates)
        return hydrated

    def _infer_observed_pod_total_from_monitor_seed(
        self, params: dict
    ) -> Optional[int]:
        seed_series = params.get("pod_monitor_series")
        if not isinstance(seed_series, list):
            return None

        observed: set[tuple[str, str]] = set()
        for item in seed_series:
            if not isinstance(item, dict):
                continue
            pod_name = str(item.get("pod_name") or "").strip()
            pod_ip = str(item.get("pod_ip") or "").strip()
            if not pod_name and not pod_ip:
                continue
            observed.add((pod_name, pod_ip))
        return len(observed) if observed else None

    def _format_run_window_label(
        self, started_at: Optional[datetime], ended_at: Optional[datetime]
    ) -> Optional[str]:
        if started_at is None and ended_at is None:
            return None

        def _format(value: Optional[datetime]) -> str:
            if value is None:
                return "-"
            return value.strftime("%Y-%m-%d %H:%M:%S")

        return f"{_format(started_at)} - {_format(ended_at)}"

    def _format_agent_progress_label(
        self,
        pod_completed: Optional[int],
        pod_actual: Optional[int],
        pod_total: Optional[int],
    ) -> Optional[str]:
        if pod_total is None and pod_actual is None and pod_completed is None:
            return None
        return f"{pod_completed or 0}/{pod_actual or 0}/{pod_total or 0}"

    def _format_summary_metrics_label(
        self,
        endpoint_total: Optional[int],
        total_requests: Optional[int],
        throughput: Optional[float],
    ) -> Optional[str]:
        parts: list[str] = []
        if endpoint_total:
            parts.append(f"{endpoint_total} 接口")
        if total_requests:
            parts.append(f"{total_requests} 请求")
        if throughput is not None:
            parts.append(f"{throughput:.1f} rep/s")
        return " / ".join(parts) if parts else None

    def _format_checks_summary_label(
        self, checks_success_rate: Optional[float], check_total: Optional[int]
    ) -> Optional[str]:
        parts: list[str] = []
        if check_total:
            parts.append(f"{check_total} 条 Checks")
        if checks_success_rate is not None:
            parts.append(f"{checks_success_rate * 100:.2f}% 成功")
        return " / ".join(parts) if parts else None

    def _label_engine_type(self, engine_type: EngineType | str | None) -> Optional[str]:
        if engine_type in {EngineType.JMETER, "jmeter"}:
            return "JMeter"
        if engine_type in {EngineType.K6, "k6"}:
            return "K6"
        if engine_type in {EngineType.CUSTOM, "custom"}:
            return "Custom"
        return None

    def _label_run_status(self, status: RunStatus | str | None) -> Optional[str]:
        if status in {RunStatus.PREPARING, "preparing"}:
            return "准备中"
        if status in {RunStatus.RUNNING, "running"}:
            return "运行中"
        if status in {RunStatus.SUCCEEDED, "succeeded"}:
            return "成功"
        if status in {RunStatus.FAILED, "failed"}:
            return "失败"
        if status in {RunStatus.STOPPED, "stopped"}:
            return "已停止"
        return None

    def list_runs(
        self,
        page: int,
        page_size: int,
        task_id: Optional[int] = None,
        task_name: Optional[str] = None,
        run_id: Optional[int] = None,
        run_status: Optional[RunStatus] = None,
        engine_type: Optional[str] = None,
        protocol: Optional[str] = None,
        business_line: Optional[str] = None,
        task_pattern: Optional[str] = None,
        env: Optional[str] = None,
        operator_name: Optional[str] = None,
        started_from: Optional[datetime] = None,
        started_to: Optional[datetime] = None,
    ) -> Tuple[List[Run], int]:
        runs, total = self.repo.find_all(
            page=page,
            page_size=page_size,
            task_id=task_id,
            task_name=task_name,
            run_id=run_id,
            run_status=run_status,
            engine_type=engine_type,
            operator_name=operator_name,
            protocol=protocol,
            business_line=business_line,
            task_pattern=task_pattern,
            env=env,
            started_from=started_from,
            started_to=started_to,
        )
        self._attach_run_display_fields(runs, include_live_runtime_enrichment=False)
        return runs, total

    def get_run(self, run_id: int) -> Optional[Run]:
        run = self.repo.find_by_id(run_id)
        if run:
            self.sync_run_terminal_status_from_agent_if_needed(run)
            self._attach_run_display_fields([run])
            run.analysis_readiness = RunAnalysisReadinessService.evaluate(run)
        return run

    @staticmethod
    def _should_sync_terminal_run_from_agent_status(run: Run) -> bool:
        if run.run_status == RunStatus.RUNNING:
            return True

        params = getattr(run, "params", None) or {}
        agent_runs = params.get("agent_runs")
        agent_run_entries = (
            [item for item in agent_runs if isinstance(item, dict)]
            if isinstance(agent_runs, list)
            else []
        )
        if run.run_status in {RunStatus.SUCCEEDED, RunStatus.STOPPED}:
            if not agent_run_entries:
                return False
            terminal_values = {
                RunStatus.SUCCEEDED.value,
                RunStatus.FAILED.value,
                RunStatus.STOPPED.value,
            }
            statuses = [
                str(item.get("status") or "").strip().lower()
                for item in agent_run_entries
            ]
            if any(status not in terminal_values for status in statuses):
                return True
            if run.run_status == RunStatus.SUCCEEDED and run.total_requests is None:
                return True
            return False

        if run.run_status != RunStatus.FAILED:
            return False

        detail = str(getattr(run, "run_status_detail", "") or "").strip().lower()
        stop_reason = str(getattr(run, "stop_reason", "") or "").strip().lower()
        if detail != "expand_failed":
            return False
        return (
            not stop_reason
            or stop_reason == "timeout"
            or stop_reason.startswith("poll_run_status_error")
        )

    def sync_run_terminal_status_from_agent_if_needed(self, run: Run) -> None:
        self._sync_terminal_run_from_agent_status(run)

    @classmethod
    def _get_terminal_sync_lock(cls, run_id: int) -> threading.Lock:
        with cls._TERMINAL_SYNC_LOCKS_GUARD:
            return cls._TERMINAL_SYNC_LOCKS.setdefault(int(run_id), threading.Lock())

    @staticmethod
    def _has_agent_execution_summary(payload: dict[str, Any]) -> bool:
        return isinstance(payload.get("jtl_summary"), dict) or isinstance(
            payload.get("k6_summary"), dict
        )

    def _normalize_agent_status_for_terminal_sync(
        self,
        payload: dict[str, Any],
    ) -> Optional[str]:
        raw_status = str(payload.get("status") or "").strip().lower()
        ended_at = payload.get("ended_at")
        pid = payload.get("pid")
        pid_missing = "pid" in payload and pid in (None, "", 0)
        has_summary = self._has_agent_execution_summary(payload)

        valid_statuses = {
            RunStatus.RUNNING.value,
            RunStatus.SUCCEEDED.value,
            RunStatus.FAILED.value,
            RunStatus.STOPPED.value,
        }
        if raw_status in valid_statuses:
            if raw_status == RunStatus.RUNNING.value and (
                ended_at or (has_summary and pid_missing)
            ):
                return RunStatus.SUCCEEDED.value
            return raw_status
        if ended_at or has_summary:
            return (
                RunStatus.FAILED.value
                if payload.get("error")
                else RunStatus.SUCCEEDED.value
            )
        return None

    def _aggregate_agent_execution_summaries(
        self,
        summaries: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        if not summaries:
            return None

        total_requests = 0
        successful_requests = 0
        failed_requests = 0
        throughput_total = 0.0
        avg_weight_total = 0.0
        avg_weight_count = 0.0
        p95_candidates: list[float] = []
        p99_candidates: list[float] = []
        max_candidates: list[float] = []
        min_candidates: list[float] = []

        for item in summaries:
            total = self._parse_seed_int(item.get("total_requests")) or 0
            successful = self._parse_seed_int(item.get("successful_requests")) or 0
            failed = self._parse_seed_int(item.get("failed_requests")) or 0
            throughput = self._parse_seed_float(
                item.get("throughput", item.get("http_reqs"))
            )
            avg = self._parse_seed_float(
                item.get("rt_avg_ms", item.get("avg_response_time"))
            )

            total_requests += max(0, total)
            successful_requests += max(0, successful)
            failed_requests += max(0, failed)
            if throughput is not None:
                throughput_total += throughput
            if avg is not None:
                weight = max(1, total)
                avg_weight_total += avg * weight
                avg_weight_count += weight

            for key, target in (
                ("rt_p95_ms", p95_candidates),
                ("p95_response_time", p95_candidates),
                ("rt_p99_ms", p99_candidates),
                ("p99_response_time", p99_candidates),
                ("rt_max_ms", max_candidates),
                ("max_response_time", max_candidates),
            ):
                value = self._parse_seed_float(item.get(key))
                if value is not None:
                    target.append(value)
            for key in ("rt_min_ms", "min_response_time"):
                value = self._parse_seed_float(item.get(key))
                if value is not None:
                    min_candidates.append(value)

        if total_requests == 0 and (successful_requests or failed_requests):
            total_requests = successful_requests + failed_requests

        aggregated: dict[str, Any] = {
            "total_requests": total_requests or None,
            "successful_requests": successful_requests,
            "failed_requests": failed_requests,
            "throughput": round(throughput_total, 4) if throughput_total else None,
            "rt_avg_ms": (
                round(avg_weight_total / avg_weight_count, 4)
                if avg_weight_count
                else None
            ),
            "rt_p95_ms": max(p95_candidates) if p95_candidates else None,
            "rt_p99_ms": max(p99_candidates) if p99_candidates else None,
            "rt_max_ms": max(max_candidates) if max_candidates else None,
            "rt_min_ms": min(min_candidates) if min_candidates else None,
        }
        if total_requests > 0:
            aggregated["error_rate"] = round(failed_requests / total_requests, 6)
            aggregated["success_rate"] = round(successful_requests / total_requests, 6)
        return aggregated

    def _apply_agent_execution_summary(
        self,
        run: Run,
        *,
        jtl_summary: Optional[dict[str, Any]],
        k6_summary: Optional[dict[str, Any]],
    ) -> None:
        summary = k6_summary if isinstance(k6_summary, dict) else jtl_summary
        if not isinstance(summary, dict):
            return

        total_requests = self._parse_seed_int(summary.get("total_requests"))
        successful_requests = self._parse_seed_int(summary.get("successful_requests"))
        failed_requests = self._parse_seed_int(summary.get("failed_requests"))
        error_rate = self._parse_seed_ratio(summary.get("error_rate"))
        success_rate = self._parse_seed_ratio(summary.get("success_rate"))

        if total_requests and failed_requests is not None:
            error_rate = max(0.0, min(1.0, failed_requests / total_requests))
        if success_rate is None and total_requests and successful_requests is not None:
            success_rate = max(0.0, min(1.0, successful_requests / total_requests))
        if success_rate is None and error_rate is not None:
            success_rate = max(0.0, min(1.0, 1 - error_rate))
        if error_rate is None and success_rate is not None:
            error_rate = max(0.0, min(1.0, 1 - success_rate))

        run.total_requests = total_requests
        run.success_rate = success_rate
        run.error_rate = error_rate
        run.avg_rt_ms = self._parse_seed_float(
            summary.get("rt_avg_ms", summary.get("avg_response_time"))
        )
        run.p95_rt_ms = self._parse_seed_float(
            summary.get("rt_p95_ms", summary.get("p95_response_time"))
        )
        run.p99_rt_ms = self._parse_seed_float(
            summary.get("rt_p99_ms", summary.get("p99_response_time"))
        )
        run.rps = self._parse_seed_float(
            summary.get("throughput", summary.get("http_reqs"))
        )

    def _build_agent_run_sync_entries(
        self,
        run: Run,
        contexts: list[tuple[str, str]],
        payloads: list[dict[str, Any]],
        statuses: list[str],
    ) -> list[dict[str, Any]]:
        existing = {
            (
                str(item.get("agent_host") or item.get("agent_ip") or ""),
                str(
                    item.get("agent_run_token")
                    or item.get("agent_token")
                    or item.get("agent_session")
                    or item.get("run_token")
                    or ""
                ),
            ): item
            for item in self._iter_agent_run_entries(run)
        }
        entries: list[dict[str, Any]] = []
        for ctx, payload, status in zip(contexts, payloads, statuses):
            host, token = ctx
            entry = dict(existing.get(ctx) or {})
            entry["agent_host"] = host
            entry["agent_run_token"] = token
            entry["status"] = status
            for key in ("agent_ip", "log_s3", "metrics_s3"):
                value = payload.get(key)
                if value not in (None, ""):
                    entry[key] = value
            ended_at = payload.get("ended_at")
            if ended_at not in (None, ""):
                entry["ended_at"] = ended_at
            entries.append(
                {key: value for key, value in entry.items() if value not in (None, "")}
            )
        return entries

    def _merge_agent_status_summary_metrics(
        self,
        payloads: list[dict[str, Any]],
    ) -> Optional[list[dict[str, Any]]]:
        rows: list[RunSummaryMetricRow] = []
        for payload in payloads:
            for summary_key in ("jtl_summary", "k6_summary"):
                summary = payload.get(summary_key)
                endpoint_rows = (
                    summary.get("endpoint_metrics")
                    if isinstance(summary, dict)
                    else None
                )
                if not isinstance(endpoint_rows, list):
                    continue
                for row in endpoint_rows:
                    if not isinstance(row, dict):
                        continue
                    endpoint_name = str(
                        row.get("endpoint_name") or row.get("name") or ""
                    ).strip()
                    if not endpoint_name:
                        continue
                    rows.append(
                        RunSummaryMetricRow(
                            endpoint_name=endpoint_name,
                            avg_rt_ms=self._parse_seed_float(row.get("avg_rt_ms")),
                            p95_rt_ms=self._parse_seed_float(row.get("p95_rt_ms")),
                            p99_rt_ms=self._parse_seed_float(row.get("p99_rt_ms")),
                            max_rt_ms=self._parse_seed_float(row.get("max_rt_ms")),
                            min_rt_ms=self._parse_seed_float(row.get("min_rt_ms")),
                            total_requests=self._parse_seed_int(
                                row.get("total_requests")
                            ),
                            throughput=self._parse_seed_float(row.get("throughput")),
                        )
                    )
        if not rows:
            return None
        return [
            row.model_dump(mode="json", exclude_none=True)
            for row in self._merge_summary_metric_rows(rows).items
        ]

    def _merge_agent_status_checks(
        self,
        payloads: list[dict[str, Any]],
    ) -> Optional[list[dict[str, Any]]]:
        rows: list[RunCheckRow] = []
        for payload in payloads:
            for summary_key in ("jtl_summary", "k6_summary"):
                summary = payload.get(summary_key)
                check_rows = (
                    summary.get("checks") if isinstance(summary, dict) else None
                )
                if not isinstance(check_rows, list):
                    continue
                for row in check_rows:
                    if not isinstance(row, dict):
                        continue
                    check_name = str(row.get("check_name") or "").strip()
                    if not check_name:
                        continue
                    rows.append(
                        RunCheckRow(
                            group_name=str(row.get("group_name") or "default"),
                            check_name=check_name,
                            success_rate=self._parse_seed_ratio(
                                row.get("success_rate")
                            ),
                        )
                    )
        if not rows:
            return None
        return [
            row.model_dump(mode="json", exclude_none=True)
            for row in self._merge_check_rows(rows).items
        ]

    def _merge_agent_status_endpoint_trends(
        self,
        payloads: list[dict[str, Any]],
    ) -> Optional[list[dict[str, Any]]]:
        responses: list[EndpointTrendResponse] = []
        for payload in payloads:
            for summary_key in ("jtl_summary", "k6_summary"):
                summary = payload.get(summary_key)
                trend_rows = (
                    summary.get("endpoint_trends")
                    if isinstance(summary, dict)
                    else None
                )
                if not isinstance(trend_rows, list):
                    continue
                items = self._parse_endpoint_trend_seed(trend_rows, None, None)
                if items:
                    responses.append(
                        EndpointTrendResponse(step_seconds=10, items=items)
                    )
        merged = self._merge_endpoint_trend_responses(responses, step_seconds=10)
        if not merged or not merged.items:
            return None
        return [
            item.model_dump(mode="json", exclude_none=True) for item in merged.items
        ]

    def _sync_terminal_run_from_agent_status(self, run: Run) -> None:
        if not self._should_sync_terminal_run_from_agent_status(run):
            return
        sync_lock = self._get_terminal_sync_lock(int(run.run_id))
        if not sync_lock.acquire(blocking=False):
            logger.debug(
                "skip terminal run sync because another request is syncing run %s",
                run.run_id,
            )
            return
        try:
            self.db.refresh(run)
            if not self._should_sync_terminal_run_from_agent_status(run):
                return
            self._sync_terminal_run_from_agent_status_locked(run)
        finally:
            sync_lock.release()

    def _sync_terminal_run_from_agent_status_locked(self, run: Run) -> None:
        agent_contexts = self._get_agent_contexts(run)
        if not agent_contexts:
            return

        payloads: list[dict[str, Any]] = []
        for ctx in agent_contexts:
            try:
                payload = self._fetch_agent_status(ctx)
            except Exception as exc:
                logger.debug(
                    "sync terminal run from agent status failed for run %s ctx=%s: %s",
                    run.run_id,
                    ctx,
                    exc,
                )
                return
            if not isinstance(payload, dict):
                return
            payloads.append(payload)

        if not payloads:
            return

        normalized_statuses: list[str] = []
        ended_candidates: list[datetime] = []
        error_candidates: list[str] = []
        for payload in payloads:
            raw_status = self._normalize_agent_status_for_terminal_sync(payload)
            if raw_status is None:
                return
            normalized_statuses.append(raw_status)
            ended_at = self._parse_ts(payload.get("ended_at"))
            if ended_at is not None:
                ended_candidates.append(ended_at)
            error_text = str(payload.get("error") or "").strip()
            if error_text:
                error_candidates.append(error_text)

        if any(status == RunStatus.RUNNING.value for status in normalized_statuses):
            return

        if any(status == RunStatus.FAILED.value for status in normalized_statuses):
            resolved_status = RunStatus.FAILED
        elif any(status == RunStatus.STOPPED.value for status in normalized_statuses):
            resolved_status = RunStatus.STOPPED
        elif all(status == RunStatus.SUCCEEDED.value for status in normalized_statuses):
            resolved_status = RunStatus.SUCCEEDED
        else:
            return

        jtl_summary = self._aggregate_agent_execution_summaries(
            [
                payload["jtl_summary"]
                for payload in payloads
                if isinstance(payload.get("jtl_summary"), dict)
            ]
        )
        k6_summary = self._aggregate_agent_execution_summaries(
            [
                payload["k6_summary"]
                for payload in payloads
                if isinstance(payload.get("k6_summary"), dict)
            ]
        )
        agent_runs = self._build_agent_run_sync_entries(
            run,
            agent_contexts,
            payloads,
            normalized_statuses,
        )

        run.run_status = resolved_status
        if resolved_status == RunStatus.FAILED:
            run.run_status_detail = "expand_failed"
            if error_candidates:
                run.stop_reason = error_candidates[0]
        elif resolved_status == RunStatus.STOPPED:
            run.run_status_detail = None
            if error_candidates:
                run.stop_reason = error_candidates[0]
        else:
            run.run_status_detail = None
            run.stop_reason = None

        ended_at = (
            max(ended_candidates) if ended_candidates else datetime.now(timezone.utc)
        )
        run.ended_at = ended_at
        started_at = self._as_utc(run.started_at)
        if started_at is not None:
            run.duration_seconds = max(0, int((ended_at - started_at).total_seconds()))
        params = dict(run.params or {})
        if agent_runs:
            params["agent_runs"] = agent_runs
            primary = agent_runs[0]
            params["agent_host"] = primary.get("agent_host")
            params["agent_run_token"] = primary.get("agent_run_token")
        for key in ("agent_ip", "log_s3", "metrics_s3"):
            value = next(
                (payload.get(key) for payload in payloads if payload.get(key)),
                None,
            )
            if value is not None:
                params[key] = value
        if jtl_summary:
            params["jtl_summary"] = jtl_summary
        if k6_summary:
            params["k6_summary"] = k6_summary
        summary_metrics = self._merge_agent_status_summary_metrics(payloads)
        if summary_metrics:
            params["summary_metrics"] = summary_metrics
        checks = self._merge_agent_status_checks(payloads)
        if checks:
            params["checks"] = checks
        endpoint_trends = self._merge_agent_status_endpoint_trends(payloads)
        if endpoint_trends and not self._extract_endpoint_trend_seed_items(params):
            params["endpoint_trends"] = endpoint_trends
        run.params = params
        self._apply_agent_execution_summary(
            run,
            jtl_summary=jtl_summary,
            k6_summary=k6_summary,
        )
        self.db.add(run)
        try:
            self.db.commit()
        except OperationalError as exc:
            self.db.rollback()
            logger.warning(
                "sync terminal run from agent status skipped after database lock for run %s: %s",
                run.run_id,
                exc,
            )
            return
        self.db.refresh(run)

    @staticmethod
    def _normalize_baseline_scope_type(
        scope_type: Optional[RunBaselineScopeType | str],
        run: Run,
    ) -> RunBaselineScopeType:
        if scope_type is None:
            return (
                RunBaselineScopeType.TASK_ENV_PROTOCOL
                if isinstance(run.protocol, str) and run.protocol.strip()
                else RunBaselineScopeType.TASK_ENV
            )
        if isinstance(scope_type, RunBaselineScopeType):
            return scope_type
        try:
            return RunBaselineScopeType(str(scope_type))
        except ValueError as exc:
            raise ValueError(f"Unsupported baseline scope_type: {scope_type}") from exc

    @staticmethod
    def _build_baseline_scope(
        run: Run, scope_type: RunBaselineScopeType
    ) -> tuple[str, int, str, Optional[str]]:
        env = str(run.env or "").strip()
        protocol = str(run.protocol or "").strip().lower() or None
        if scope_type == RunBaselineScopeType.TASK_ENV:
            return f"task:{run.task_id}|env:{env}", run.task_id, env, None
        if scope_type == RunBaselineScopeType.TASK_ENV_PROTOCOL:
            if not protocol:
                raise ValueError(
                    "Baseline scope task_env_protocol requires run.protocol"
                )
            return (
                f"task:{run.task_id}|env:{env}|protocol:{protocol}",
                run.task_id,
                env,
                protocol,
            )
        raise ValueError(f"Unsupported baseline scope_type: {scope_type}")

    @staticmethod
    def _build_baseline_scope_label(
        scope_type: RunBaselineScopeType, env: str, protocol: Optional[str]
    ) -> str:
        if scope_type == RunBaselineScopeType.TASK_ENV:
            return f"task + env ({env})"
        if scope_type == RunBaselineScopeType.TASK_ENV_PROTOCOL:
            return f"task + env + protocol ({env} / {protocol or '-'})"
        return scope_type.value

    def _serialize_run_baseline(
        self, baseline: RunBaseline, current_run_id: Optional[int] = None
    ) -> RunBaselineResponse:
        baseline_run = self.repo.find_by_id(int(baseline.baseline_run_id))
        baseline_run_summary = None
        if baseline_run:
            self._attach_run_display_fields(
                [baseline_run], include_live_runtime_enrichment=False
            )
            baseline_run_summary = RunBaselineRunSummary(
                run_id=baseline_run.run_id,
                task_id=baseline_run.task_id,
                task_name=baseline_run.task_name,
                env=baseline_run.env,
                protocol=baseline_run.protocol,
                run_status=baseline_run.run_status,
                started_at=baseline_run.started_at,
                ended_at=baseline_run.ended_at,
            )

        scope_type = RunBaselineScopeType(str(baseline.scope_type))
        protocol = str(baseline.protocol).strip() if baseline.protocol else None
        return RunBaselineResponse(
            baseline_id=int(baseline.baseline_id),
            scope_type=scope_type,
            scope_key=str(baseline.scope_key),
            scope_label=self._build_baseline_scope_label(
                scope_type, str(baseline.env), protocol
            ),
            task_id=int(baseline.task_id),
            env=str(baseline.env),
            protocol=protocol,
            baseline_run_id=int(baseline.baseline_run_id),
            baseline_source=RunBaselineSource(str(baseline.baseline_source)),
            effective_from=baseline.effective_from,
            note=baseline.note,
            current_run_id=current_run_id,
            current_run_matches_baseline=bool(
                current_run_id and int(current_run_id) == int(baseline.baseline_run_id)
            ),
            baseline_run=baseline_run_summary,
        )

    def get_run_baseline(
        self,
        run_id: int,
        scope_type: Optional[RunBaselineScopeType | str] = None,
    ) -> Optional[RunBaselineResponse]:
        run = self.repo.find_by_id(run_id)
        if not run:
            return None
        normalized_scope = self._normalize_baseline_scope_type(scope_type, run)
        scope_key, _task_id, _env, _protocol = self._build_baseline_scope(
            run, normalized_scope
        )
        baseline = (
            self.db.query(RunBaseline)
            .filter(
                RunBaseline.scope_type == normalized_scope.value,
                RunBaseline.scope_key == scope_key,
            )
            .first()
        )
        if not baseline:
            return None
        return self._serialize_run_baseline(baseline, current_run_id=run.run_id)

    def set_run_baseline(
        self, run_id: int, payload: RunBaselineSetRequest
    ) -> RunBaselineResponse:
        run = self.repo.find_by_id(run_id)
        if not run:
            raise ValueError("Run not found")
        if not self._is_terminal_run_status(run.run_status):
            status = str(getattr(run.run_status, "value", run.run_status) or "unknown")
            raise ValueError(f"只能将已结束的 Run 设为基线，当前状态：{status}")
        normalized_scope = self._normalize_baseline_scope_type(payload.scope_type, run)
        scope_key, task_id, env, protocol = self._build_baseline_scope(
            run, normalized_scope
        )
        baseline = (
            self.db.query(RunBaseline)
            .filter(
                RunBaseline.scope_type == normalized_scope.value,
                RunBaseline.scope_key == scope_key,
            )
            .first()
        )
        if baseline is None:
            baseline = RunBaseline(
                scope_type=normalized_scope.value,
                scope_key=scope_key,
                task_id=task_id,
                env=env,
                protocol=protocol,
                baseline_run_id=run.run_id,
                baseline_source=payload.baseline_source.value,
                effective_from=datetime.now(timezone.utc),
                note=payload.note,
            )
            self.db.add(baseline)
        else:
            baseline.task_id = task_id
            baseline.env = env
            baseline.protocol = protocol
            baseline.baseline_run_id = run.run_id
            baseline.baseline_source = payload.baseline_source.value
            baseline.effective_from = datetime.now(timezone.utc)
            baseline.note = payload.note

        self.db.commit()
        self.db.refresh(baseline)
        return self._serialize_run_baseline(baseline, current_run_id=run.run_id)

    @staticmethod
    def _verdict_rank(verdict: str) -> int:
        return {"pass": 0, "warn": 1, "fail": 2}.get(verdict, 99)

    @classmethod
    def _merge_verdict(cls, current: str, next_verdict: str) -> str:
        return (
            next_verdict
            if cls._verdict_rank(next_verdict) > cls._verdict_rank(current)
            else current
        )

    @staticmethod
    def _resolve_verdict_metric_snapshot(run: Run) -> dict[str, Optional[float]]:
        summary = getattr(run, "overview_summary", None)
        error_rate = run.error_rate
        if error_rate is None and run.success_rate is not None:
            error_rate = max(0.0, min(1.0, 1 - run.success_rate))
        return {
            "throughput": (
                summary.throughput
                if summary and summary.throughput is not None
                else run.rps
            ),
            "error_rate": error_rate,
            "avg_rt_ms": (
                summary.avg_rt_ms
                if summary and summary.avg_rt_ms is not None
                else run.avg_rt_ms
            ),
            "p95_rt_ms": (
                summary.p95_rt_ms
                if summary and summary.p95_rt_ms is not None
                else run.p95_rt_ms
            ),
            "p99_rt_ms": run.p99_rt_ms,
        }

    @staticmethod
    def _parse_optional_threshold(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    def get_run_verdict(self, run_id: int) -> Optional[RunVerdictResponse]:
        run = self.repo.find_by_id(run_id)
        if not run:
            return None
        self._attach_run_display_fields([run], include_live_runtime_enrichment=False)

        verdict = "pass"
        reason_codes: list[str] = []
        metric_deltas: list[RunVerdictMetricDelta] = []
        baseline = self.get_run_baseline(run_id)
        params = run.params or {}

        current_metrics = self._resolve_verdict_metric_snapshot(run)

        if run.run_status in {RunStatus.PREPARING, RunStatus.RUNNING}:
            verdict = self._merge_verdict(verdict, "warn")
            reason_codes.append("run_not_terminal")
        elif run.run_status == RunStatus.FAILED:
            verdict = self._merge_verdict(verdict, "fail")
            reason_codes.append("run_failed")

        error_rate = current_metrics["error_rate"]
        if error_rate is not None:
            metric_deltas.append(
                RunVerdictMetricDelta(
                    metric="error_rate",
                    unit="ratio",
                    current_value=round(error_rate, 6),
                )
            )
            if error_rate >= 0.05:
                verdict = self._merge_verdict(verdict, "fail")
                reason_codes.append("error_rate_fail_threshold")
            elif error_rate >= 0.01:
                verdict = self._merge_verdict(verdict, "warn")
                reason_codes.append("error_rate_warn_threshold")

        p95_threshold = self._parse_optional_threshold(params.get("p95_threshold_ms"))
        p95_value = current_metrics["p95_rt_ms"]
        if (
            p95_threshold is not None
            and p95_value is not None
            and p95_value > p95_threshold
        ):
            verdict = self._merge_verdict(verdict, "fail")
            reason_codes.append("p95_threshold_exceeded")

        if baseline and baseline.baseline_run:
            baseline_metrics = self._resolve_verdict_metric_snapshot(
                self.repo.find_by_id(baseline.baseline_run.run_id) or run
            )
            threshold_table = {
                "throughput": {"warn": -0.10, "fail": -0.20, "unit": "rep/s"},
                "avg_rt_ms": {"warn": 0.10, "fail": 0.20, "unit": "ms"},
                "p95_rt_ms": {"warn": 0.10, "fail": 0.20, "unit": "ms"},
                "p99_rt_ms": {"warn": 0.15, "fail": 0.30, "unit": "ms"},
            }
            for metric, config in threshold_table.items():
                current_value = current_metrics.get(metric)
                baseline_value = baseline_metrics.get(metric)
                if current_value is None or baseline_value in (None, 0):
                    continue
                delta_value = current_value - baseline_value
                delta_ratio = delta_value / baseline_value
                metric_deltas.append(
                    RunVerdictMetricDelta(
                        metric=metric,
                        unit=str(config["unit"]),
                        current_value=round(current_value, 4),
                        baseline_value=round(baseline_value, 4),
                        delta_value=round(delta_value, 4),
                        delta_ratio=round(delta_ratio, 6),
                    )
                )
                if metric == "throughput":
                    if delta_ratio <= float(config["fail"]):
                        verdict = self._merge_verdict(verdict, "fail")
                        reason_codes.append("throughput_degraded_fail")
                    elif delta_ratio <= float(config["warn"]):
                        verdict = self._merge_verdict(verdict, "warn")
                        reason_codes.append("throughput_degraded_warn")
                else:
                    if delta_ratio >= float(config["fail"]):
                        verdict = self._merge_verdict(verdict, "fail")
                        reason_codes.append(f"{metric}_regression_fail")
                    elif delta_ratio >= float(config["warn"]):
                        verdict = self._merge_verdict(verdict, "warn")
                        reason_codes.append(f"{metric}_regression_warn")

        summary_map = {
            "pass": "当前 run 通过最小稳定性判断",
            "warn": "当前 run 存在需人工复核的波动或未终态情况",
            "fail": "当前 run 已触发失败级规则，建议优先排查",
        }
        if not reason_codes:
            reason_codes.append("no_rule_triggered")

        return RunVerdictResponse(
            run_id=run.run_id,
            verdict=verdict,
            summary_text=summary_map.get(verdict, "当前 run verdict 未知"),
            reason_codes=reason_codes,
            baseline_run_id=baseline.baseline_run_id if baseline else None,
            baseline_scope_type=baseline.scope_type if baseline else None,
            baseline_scope_label=baseline.scope_label if baseline else None,
            metric_deltas=metric_deltas,
        )

    def get_run_ai_analyst(self, run_id: int) -> Optional[RunAIAnalystResponse]:
        run = self.repo.find_by_id(run_id)
        if not run:
            return None
        self._attach_run_display_fields([run], include_live_runtime_enrichment=False)

        verdict = self.get_run_verdict(run_id)
        summary_metrics = self.get_summary_metrics(run_id)
        dashboards = self.get_dashboards(run_id)
        baseline = self.get_run_baseline(run_id)
        if verdict is None:
            return None

        input_sources = ["verdict"]
        if summary_metrics.items:
            input_sources.append("summary_metrics")
        if baseline:
            input_sources.append("baseline")
        if dashboards.summary.total_dashboard_count > 0:
            input_sources.append("dashboards")
        if run.stop_reason or run.run_status_detail:
            input_sources.append("runtime_detail")

        key_findings: list[RunAIInsightItem] = []
        recommended_actions: list[str] = []
        limitations: list[str] = []
        top_delta = self._select_run_ai_top_metric_delta(verdict)
        primary_dashboard = dashboards.items[0] if dashboards.items else None

        total_requests, throughput, avg_rt_ms, p95_rt_ms, _p99_rt_ms, endpoint_total = (
            self._aggregate_summary_metric_rows(summary_metrics.items)
        )
        throughput_peak = self._resolve_run_endpoint_throughput_peak(run)

        key_findings.append(
            RunAIInsightItem(
                label="总体结论",
                detail=verdict.summary_text,
            )
        )
        if summary_metrics.items:
            metric_parts: list[str] = []
            if throughput is not None:
                metric_parts.append(f"吞吐 {throughput:.2f} rep/s")
            if throughput_peak and throughput_peak.get("peak_qps") is not None:
                metric_parts.append(
                    f"峰值 QPS {float(throughput_peak['peak_qps']):.2f} req/s"
                )
            if avg_rt_ms is not None:
                metric_parts.append(f"平均 RT {avg_rt_ms:.2f} ms")
            if p95_rt_ms is not None:
                metric_parts.append(f"P95 {p95_rt_ms:.2f} ms")
            if run.error_rate is not None:
                metric_parts.append(f"错误率 {run.error_rate * 100:.2f}%")
            if total_requests is not None:
                metric_parts.append(f"总请求 {total_requests}")
            if endpoint_total > 0:
                metric_parts.append(f"接口 {endpoint_total} 个")
            if metric_parts:
                key_findings.append(
                    RunAIInsightItem(
                        label="指标摘要",
                        detail=" / ".join(metric_parts),
                    )
                )
        if baseline and baseline.baseline_run_id:
            key_findings.append(
                RunAIInsightItem(
                    label="基线范围",
                    detail=f"{baseline.scope_label} / baseline #{baseline.baseline_run_id}",
                )
            )
        if top_delta is not None:
            key_findings.append(
                RunAIInsightItem(
                    label="主要差异",
                    detail=self._format_run_verdict_metric_delta(top_delta),
                )
            )
        if dashboards.summary.total_dashboard_count > 0:
            dashboard_parts: list[str] = []
            if dashboards.summary.has_engine_grafana:
                dashboard_parts.append("Engine Grafana")
            if dashboards.summary.has_pod_grafana:
                dashboard_parts.append("执行节点监控")
            if dashboards.summary.related_monitor_total > 0:
                dashboard_parts.append(
                    f"{dashboards.summary.related_monitor_total} 个业务监控"
                )
            if dashboards.summary.topology_total > 0:
                dashboard_parts.append(
                    f"{dashboards.summary.topology_total} 个拓扑入口"
                )
            key_findings.append(
                RunAIInsightItem(
                    label="观测入口",
                    detail=f"当前可用 dashboard {dashboards.summary.total_dashboard_count} 个"
                    + (f"（{' / '.join(dashboard_parts)}）" if dashboard_parts else ""),
                )
            )
        if run.run_status_detail:
            key_findings.append(
                RunAIInsightItem(
                    label="运行时细节",
                    detail=f"run_status_detail={run.run_status_detail}",
                )
            )
        if run.stop_reason:
            key_findings.append(
                RunAIInsightItem(
                    label="停止/失败原因",
                    detail=run.stop_reason,
                )
            )

        if top_delta is not None:
            recommended_actions.append(
                f"优先复核 {self._format_run_verdict_metric_delta(top_delta)} 对应的 RunDetail 指标面板。"
            )
        else:
            recommended_actions.append(
                "先复核 RunDetail 的基础指标、日志和 dashboard 前门。"
            )

        if run.run_status == RunStatus.RUNNING:
            recommended_actions.append(
                "当前 run 尚未结束，建议待终态后再做最终稳定性判断。"
            )
        if dashboards.summary.total_dashboard_count > 0:
            recommended_actions.append(
                "优先打开关联监控 / dashboard，确认异常是否在外部观测面同步出现。"
            )
            primary_dashboard = dashboards.items[0] if dashboards.items else None
            if primary_dashboard is not None:
                recommended_actions.append(
                    f"建议先打开「{primary_dashboard.title}」确认异常是否在监控面同步出现。"
                )
        if run.stop_reason:
            recommended_actions.append(
                "结合 stop_reason 与 agent/runtime 日志，先排查是否为运行环境问题。"
            )
        if not baseline:
            recommended_actions.append(
                "先为当前任务设置 baseline，避免后续结论长期停留在绝对阈值判断。"
            )

        confidence = "medium"
        if (
            verdict.verdict == "fail"
            and baseline
            and summary_metrics.items
            and dashboards.summary.total_dashboard_count > 0
        ):
            confidence = "high"
        elif (
            not baseline
            or not summary_metrics.items
            or run.run_status == RunStatus.RUNNING
        ):
            confidence = "low"

        if not baseline:
            limitations.append(
                "当前缺少 baseline，只能基于绝对阈值和当前 run 现象做初步判断。"
            )
        if not summary_metrics.items:
            limitations.append(
                "当前缺少稳定的 summary_metrics，只能依赖 run 概览与规则结果做摘要。"
            )
        if dashboards.summary.total_dashboard_count == 0:
            limitations.append("当前没有可用 dashboard 前门，观测链不完整。")
        else:
            limitations.append(
                "dashboard/frontdoor 证据仅说明外部 URL 或入口已配置；AI 未读取外部 dashboard、SkyWalking trace 或 topology 内容。"
            )
        if run.run_status == RunStatus.RUNNING:
            limitations.append("当前 run 尚未终态，结论不应视为最终验收结论。")

        observability_query_result = (
            ObservabilityQueryService().build_evidence_from_params(run.params)
        )
        limitations.extend(observability_query_result.limitations)

        summary_map = {
            "pass": "AI 初判：当前 run 未触发明显异常，summary_metrics 与 verdict 基本一致，可作为通过候选，但仍建议结合关键 frontdoor 再做最终确认。",
            "warn": "AI 初判：当前 run 存在需人工复核的波动，建议优先看基线差异、summary_metrics 和关键日志。",
            "fail": "AI 初判：当前 run 已触发失败级信号，建议按指标差异、dashboard/frontdoor 和运行时细节优先排查。",
        }
        primary_focus = self._build_run_ai_primary_focus(
            run=run,
            top_delta=top_delta,
            primary_dashboard=primary_dashboard,
            baseline=baseline,
        )
        evidence_pack = self._build_run_ai_evidence_pack(
            run=run,
            verdict=verdict,
            summary_metrics=summary_metrics,
            dashboards=dashboards,
            baseline=baseline,
            top_delta=top_delta,
            throughput_peak=throughput_peak,
            observability_query_result=observability_query_result,
        )
        for item in evidence_pack:
            if item.source not in input_sources:
                input_sources.append(item.source)
        root_cause_hypotheses = self._build_run_ai_root_cause_hypotheses(
            run=run,
            verdict=verdict,
            baseline=baseline,
            top_delta=top_delta,
            evidence=evidence_pack,
        )

        return RunAIAnalystResponse(
            run_id=run_id,
            verdict=verdict.verdict,
            analyst_summary=summary_map.get(verdict.verdict, verdict.summary_text),
            confidence=confidence,
            input_sources=input_sources,
            primary_focus=primary_focus,
            key_findings=key_findings,
            evidence_pack=evidence_pack,
            root_cause_hypotheses=root_cause_hypotheses,
            recommended_actions=recommended_actions,
            limitations=limitations,
        )

    def _build_run_ai_evidence_pack(
        self,
        *,
        run: Run,
        verdict: RunVerdictResponse,
        summary_metrics: RunSummaryMetricsResponse,
        dashboards: RunDashboardsResponse,
        baseline: Optional[RunBaselineResponse],
        top_delta: Optional[RunVerdictMetricDelta],
        throughput_peak: Optional[dict[str, Any]] = None,
        observability_query_result: Optional[ObservabilityQueryResult] = None,
    ) -> list[RunAIEvidenceItem]:
        evidence: list[RunAIEvidenceItem] = [
            RunAIEvidenceItem(
                source="verdict",
                label="规则判定",
                detail=(
                    f"{verdict.summary_text}"
                    + (
                        f"；reason_codes={', '.join(verdict.reason_codes[:4])}"
                        if verdict.reason_codes
                        else ""
                    )
                ),
                severity=verdict.verdict,
                target_section="summary_metrics",
            )
        ]

        self._append_run_ai_summary_metric_evidence(evidence, summary_metrics)
        if baseline and top_delta is not None:
            evidence.append(
                RunAIEvidenceItem(
                    source="baseline",
                    label="基线差异",
                    detail=self._format_run_verdict_metric_delta(top_delta),
                    metric=top_delta.metric,
                    severity=verdict.verdict,
                    target_section="baseline",
                )
            )
        if dashboards.summary.total_dashboard_count > 0:
            frontdoor_parts = self._format_run_ai_dashboard_frontdoors(dashboards)
            evidence.append(
                RunAIEvidenceItem(
                    source="dashboards",
                    label="观测入口",
                    detail=(
                        f"dashboard_total={dashboards.summary.total_dashboard_count}"
                        f"，engine={int(dashboards.summary.has_engine_grafana)}"
                        f"，pod={int(dashboards.summary.has_pod_grafana)}"
                        f"，business={dashboards.summary.related_monitor_total}"
                        f"，topology={dashboards.summary.topology_total}"
                        f"，config={dashboards.summary.server_config_total}"
                        + (
                            f"，frontdoors={'; '.join(frontdoor_parts)}"
                            if frontdoor_parts
                            else ""
                        )
                    ),
                    target_section="monitor",
                )
            )
        if observability_query_result is not None:
            evidence.extend(observability_query_result.evidence)

        endpoint_trend_evidence = self._build_run_ai_endpoint_trend_evidence(
            run, throughput_peak=throughput_peak
        )
        if endpoint_trend_evidence is not None:
            evidence.append(endpoint_trend_evidence)

        log_evidence = self._build_run_ai_log_evidence(run)
        if log_evidence is not None:
            evidence.append(log_evidence)

        pod_monitor_evidence = self._build_run_ai_pod_monitor_evidence(run)
        if pod_monitor_evidence is not None:
            evidence.append(pod_monitor_evidence)

        alert_evidence = self._build_run_ai_external_alert_evidence(run)
        if alert_evidence is not None:
            evidence.append(alert_evidence)

        return evidence

    @staticmethod
    def _format_run_ai_dashboard_frontdoors(
        dashboards: RunDashboardsResponse,
    ) -> list[str]:
        frontdoors: list[str] = []
        prioritized_items = sorted(
            dashboards.items,
            key=RunService._run_ai_dashboard_frontdoor_priority,
        )
        for item in prioritized_items[:3]:
            dashboard_type = (
                item.dashboard_type.value
                if hasattr(item.dashboard_type, "value")
                else str(item.dashboard_type)
            )
            title = item.title.strip() if item.title else "未命名入口"
            provider = RunService._infer_run_ai_dashboard_provider(item)
            embed_mode = item.embed_mode or "new_tab"
            url_present = int(bool(item.url and item.url.strip()))
            frontdoors.append(
                f"{dashboard_type}:{title}"
                f"(provider={provider}, embed={embed_mode}, url_present={url_present})"
            )
        return frontdoors

    @staticmethod
    def _run_ai_dashboard_frontdoor_priority(item: RunDashboardLink) -> int:
        title = (item.title or "").lower()
        if "trace" in title or "链路入口" in title:
            return 0
        if item.dashboard_type == RunDashboardType.TOPOLOGY:
            return 1
        if item.dashboard_type == RunDashboardType.RELATED_MONITOR:
            return 2
        if item.dashboard_type == RunDashboardType.SERVER_CONFIG:
            return 3
        return 4

    @staticmethod
    def _infer_run_ai_dashboard_provider(item: RunDashboardLink) -> str:
        haystack = f"{item.title} {item.url}".lower()
        for provider in ("skywalking", "grafana", "prometheus", "zipkin"):
            if provider in haystack:
                return provider
        if item.dashboard_type in {
            RunDashboardType.ENGINE_GRAFANA,
            RunDashboardType.POD_GRAFANA,
        }:
            return "grafana"
        return "external"

    def _build_run_ai_root_cause_hypotheses(
        self,
        *,
        run: Run,
        verdict: RunVerdictResponse,
        baseline: Optional[RunBaselineResponse],
        top_delta: Optional[RunVerdictMetricDelta],
        evidence: list[RunAIEvidenceItem],
    ) -> list[RunAIRootCauseHypothesis]:
        evidence_refs = [self._format_run_ai_evidence_ref(item) for item in evidence]
        refs_by_source: dict[str, list[str]] = {}
        for item in evidence:
            refs_by_source.setdefault(item.source, []).append(
                self._format_run_ai_evidence_ref(item)
            )
        candidates: list[RunAIRootCauseHypothesis] = []

        runtime_reason = run.stop_reason or run.run_status_detail
        if runtime_reason:
            refs = [
                ref
                for source, refs_for_source in refs_by_source.items()
                if source in {"runtime_logs", "verdict"}
                for ref in refs_for_source
            ]
            candidates.append(
                RunAIRootCauseHypothesis(
                    category="runtime_environment",
                    hypothesis=(
                        "运行时或 agent 环境异常可能先于业务性能问题触发，"
                        f"当前线索为 {runtime_reason}。"
                    ),
                    confidence="medium",
                    confidence_score=0.74,
                    severity="warn" if verdict.verdict != "fail" else "fail",
                    evidence_refs=refs or evidence_refs[:2],
                    next_actions=[
                        "先复核 runtime_logs 与 agent/pod 生命周期，确认是否存在调度、启动或主动停止异常。",
                        "若运行时异常成立，本轮性能结论应降级为环境问题，不直接归因到被测服务。",
                    ],
                )
            )

        if top_delta is not None:
            metric = top_delta.metric
            metric_refs = [
                ref
                for source, refs_for_source in refs_by_source.items()
                if source
                in {
                    "baseline",
                    "summary_metrics",
                    "endpoint_trends",
                    "dashboards",
                    "observability_queries",
                }
                for ref in refs_for_source
            ]
            if metric == "throughput":
                hypothesis = (
                    "吞吐相对基线下降，可能来自服务限流、下游排队或压测端供给不足。"
                )
                category = "throughput_degradation"
                next_actions = [
                    "对照 endpoint_trends 与业务 dashboard，确认吞吐下降是否集中在少数接口。",
                    "同步检查 pod_monitor，排除执行节点 CPU、内存或网络资源打满导致的假性吞吐下降。",
                ]
            else:
                hypothesis = (
                    f"{metric} 相对基线升高，可能来自接口处理耗时增加、"
                    "下游依赖排队或资源竞争。"
                )
                category = "latency_regression"
                next_actions = [
                    "优先打开慢接口 TopN 和对应 dashboard，确认延迟抬升是否与业务监控峰值同窗。",
                    "按接口维度回看错误日志和下游依赖指标，避免只用总体均值判断根因。",
                ]
            score = 0.82 if baseline and verdict.verdict == "fail" else 0.68
            candidates.append(
                RunAIRootCauseHypothesis(
                    category=category,
                    hypothesis=hypothesis,
                    confidence="high" if score >= 0.8 else "medium",
                    confidence_score=score,
                    severity=verdict.verdict,
                    metric=metric,
                    evidence_refs=metric_refs or evidence_refs[:3],
                    next_actions=next_actions,
                )
            )

        if not candidates and verdict.verdict == "pass":
            return []

        pod_monitor_evidence = [
            item
            for item in evidence
            if item.source == "pod_monitor" and item.severity in {"warn", "fail"}
        ]
        if pod_monitor_evidence:
            candidates.append(
                RunAIRootCauseHypothesis(
                    category="load_generator_resource",
                    hypothesis=(
                        "执行节点资源峰值可能影响本轮结果可信度，需先排除执行端瓶颈。"
                    ),
                    confidence="medium",
                    confidence_score=0.66,
                    severity="warn",
                    evidence_refs=[
                        self._format_run_ai_evidence_ref(item)
                        for item in pod_monitor_evidence
                    ],
                    next_actions=[
                        "检查执行节点 CPU、内存、网络与 socket 峰值是否贴近资源上限。",
                        "必要时用更低并发或更多 pod 复跑，验证异常是否随执行端资源变化而消失。",
                    ],
                )
            )

        if not candidates and verdict.verdict in {"warn", "fail"}:
            candidates.append(
                RunAIRootCauseHypothesis(
                    category="insufficient_evidence",
                    hypothesis=(
                        "当前规则已触发异常，但 evidence pack 尚不足以稳定区分业务根因与观测缺口。"
                    ),
                    confidence="low",
                    confidence_score=0.38,
                    severity=verdict.verdict,
                    evidence_refs=evidence_refs[:3],
                    next_actions=[
                        "补齐 baseline、summary_metrics、dashboard 和异常日志后再提升根因置信度。",
                        "先按 primary_focus 指向的面板做一次人工复核。",
                    ],
                    limitations=["缺少足够的交叉证据，候选仅作为排查入口。"],
                )
            )

        if not baseline:
            limitation = "当前缺少 baseline，所有根因候选置信度已被下调。"
            for candidate in candidates:
                if limitation not in candidate.limitations:
                    candidate.limitations.append(limitation)
                candidate.confidence_score = min(candidate.confidence_score, 0.62)
                if candidate.confidence == "high":
                    candidate.confidence = "medium"

        if "observability_queries" in refs_by_source and candidates:
            query_refs = refs_by_source["observability_queries"]
            for candidate in candidates:
                missing_refs = [
                    ref for ref in query_refs if ref not in candidate.evidence_refs
                ]
                if missing_refs:
                    candidate.evidence_refs.extend(missing_refs)
                    break

        return candidates[:4]

    @staticmethod
    def _format_run_ai_evidence_ref(item: RunAIEvidenceItem) -> str:
        metric = f"/{item.metric}" if item.metric else ""
        return f"{item.source}:{item.label}{metric}"

    def _append_run_ai_summary_metric_evidence(
        self,
        evidence: list[RunAIEvidenceItem],
        summary_metrics: RunSummaryMetricsResponse,
    ) -> None:
        if not summary_metrics.items:
            return

        total_requests, throughput, avg_rt_ms, p95_rt_ms, _p99_rt_ms, endpoint_total = (
            self._aggregate_summary_metric_rows(summary_metrics.items)
        )
        parts: list[str] = [f"endpoint_total={endpoint_total}"]
        if total_requests is not None:
            parts.append(f"total_requests={total_requests}")
        if throughput is not None:
            parts.append(f"throughput={throughput:.2f} rep/s")
        if avg_rt_ms is not None:
            parts.append(f"avg_rt={avg_rt_ms:.2f} ms")
        if p95_rt_ms is not None:
            parts.append(f"p95={p95_rt_ms:.2f} ms")
        evidence.append(
            RunAIEvidenceItem(
                source="summary_metrics",
                label="接口聚合指标",
                detail="，".join(parts),
                target_section="summary_metrics",
            )
        )

        slow_rows = sorted(
            [
                row
                for row in summary_metrics.items
                if row.endpoint_name != "overall"
                and (
                    isinstance(row.p95_rt_ms, (int, float))
                    or isinstance(row.avg_rt_ms, (int, float))
                )
            ],
            key=lambda row: float(row.p95_rt_ms or row.avg_rt_ms or 0),
            reverse=True,
        )
        if slow_rows:
            top_rows = slow_rows[:3]
            evidence.append(
                RunAIEvidenceItem(
                    source="summary_metrics",
                    label="慢接口 TopN",
                    detail="；".join(
                        f"{row.endpoint_name}: p95={row.p95_rt_ms if row.p95_rt_ms is not None else 'n/a'} ms"
                        f", avg={row.avg_rt_ms if row.avg_rt_ms is not None else 'n/a'} ms"
                        for row in top_rows
                    ),
                    target_section="summary_metrics",
                    metric="rt_p95_ms",
                )
            )

    def _resolve_run_endpoint_throughput_peak(
        self, run: Run
    ) -> Optional[dict[str, Any]]:
        try:
            trends = self.get_endpoint_trends(
                run.run_id,
                metric=EndpointTrendMetric.THROUGHPUT.value,
                step_seconds=10,
            )
        except Exception as exc:  # pragma: no cover - evidence is best-effort
            logger.debug("run ai endpoint throughput peak skipped: %s", exc)
            return None
        if not trends.items:
            return None

        endpoint_total = len({item.endpoint_name for item in trends.items})
        point_total = sum(len(item.points) for item in trends.items)
        throughput_by_ts: dict[Any, float] = {}
        peak_ts = None
        peak_qps: Optional[float] = None
        for item in trends.items:
            for point in item.points:
                if point.value is None:
                    continue
                timestamp = point.ts
                throughput_by_ts[timestamp] = throughput_by_ts.get(
                    timestamp, 0.0
                ) + float(point.value)
        for timestamp, value in throughput_by_ts.items():
            if peak_qps is None or value > peak_qps:
                peak_qps = value
                peak_ts = timestamp
        if peak_qps is None:
            return None

        return {
            "peak_qps": round(float(peak_qps), 4),
            "peak_ts": (
                peak_ts.isoformat() if hasattr(peak_ts, "isoformat") else str(peak_ts)
            ),
            "endpoint_total": endpoint_total,
            "point_total": point_total,
        }

    def _build_run_ai_endpoint_trend_evidence(
        self,
        run: Run,
        *,
        throughput_peak: Optional[dict[str, Any]] = None,
    ) -> Optional[RunAIEvidenceItem]:
        try:
            trends = self.get_endpoint_trends(
                run.run_id,
                metric=EndpointTrendMetric.THROUGHPUT.value,
                step_seconds=10,
            )
        except Exception as exc:  # pragma: no cover - evidence is best-effort
            logger.debug("run ai endpoint trend evidence skipped: %s", exc)
            return None
        if not trends.items:
            return None

        endpoint_total = len({item.endpoint_name for item in trends.items})
        point_total = sum(len(item.points) for item in trends.items)
        latest_values: list[str] = []
        for item in trends.items[:3]:
            latest_point = item.points[-1] if item.points else None
            if latest_point and latest_point.value is not None:
                latest_values.append(
                    f"{item.endpoint_name}={latest_point.value:.2f} {item.unit}"
                )
        peak = throughput_peak or self._resolve_run_endpoint_throughput_peak(run)
        peak_parts = ""
        if peak and peak.get("peak_qps") is not None:
            peak_parts = f"，peak_qps={float(peak['peak_qps']):.2f} req/s" + (
                f"，peak_ts={peak.get('peak_ts')}" if peak.get("peak_ts") else ""
            )
        return RunAIEvidenceItem(
            source="endpoint_trends",
            label="吞吐峰值",
            detail=(
                f"metric=throughput，endpoint_total={endpoint_total}，points={point_total}"
                + peak_parts
                + (f"，latest: {'；'.join(latest_values)}" if latest_values else "")
            ),
            target_section="summary_metrics",
            metric="peak_qps",
        )

    def _build_run_ai_log_evidence(self, run: Run) -> Optional[RunAIEvidenceItem]:
        try:
            logs = self.get_logs(
                run.run_id,
                view="exception",
                limit=3,
                order="desc",
            )
        except Exception as exc:  # pragma: no cover - evidence is best-effort
            logger.debug("run ai log evidence skipped: %s", exc)
            return None
        if not logs.items:
            return None

        samples = [
            f"{item.level}:{item.message[:120]}"
            for item in logs.items[:3]
            if item.message
        ]
        if not samples:
            return None
        return RunAIEvidenceItem(
            source="runtime_logs",
            label="异常日志样本",
            detail="；".join(samples),
            target_section="runtime_logs",
            severity="warn",
        )

    def _build_run_ai_pod_monitor_evidence(
        self, run: Run
    ) -> Optional[RunAIEvidenceItem]:
        try:
            monitor = self.get_pods_monitor(run.run_id, step_seconds=10)
        except Exception as exc:  # pragma: no cover - evidence is best-effort
            logger.debug("run ai pod monitor evidence skipped: %s", exc)
            return None
        if not monitor.series:
            return None

        summary = monitor.summary
        parts = [f"observed_pods={summary.observed_pod_total}"]
        if summary.cpu_summary_label:
            parts.append(f"cpu={summary.cpu_summary_label}")
        if summary.memory_summary_label:
            parts.append(f"memory={summary.memory_summary_label}")
        if summary.network_summary_label:
            parts.append(f"network={summary.network_summary_label}")
        if summary.runtime_summary_label:
            parts.append(f"runtime={summary.runtime_summary_label}")
        return RunAIEvidenceItem(
            source="pod_monitor",
            label="执行节点资源摘要",
            detail="，".join(parts),
            target_section="monitor",
            severity=(
                "warn"
                if (
                    summary.cpu_usage_peak_percent
                    and summary.cpu_usage_peak_percent >= 80
                )
                or (
                    summary.memory_usage_peak_percent
                    and summary.memory_usage_peak_percent >= 80
                )
                else None
            ),
        )

    def _build_run_ai_external_alert_evidence(
        self, run: Run
    ) -> Optional[RunAIEvidenceItem]:
        try:
            summary = AlertEventService(self.db).summarize_for_run(run.run_id, limit=5)
        except Exception as exc:  # pragma: no cover - evidence is best-effort
            logger.debug("run ai external alert evidence skipped: %s", exc)
            return None
        if not summary:
            return None
        parts = [f"received={summary['total']}"]
        severities = summary.get("severities") or {}
        statuses = summary.get("statuses") or {}
        samples = summary.get("samples") or []
        if severities:
            parts.append(
                "severity="
                + ",".join(f"{key}:{value}" for key, value in severities.items())
            )
        if statuses:
            parts.append(
                "status="
                + ",".join(f"{key}:{value}" for key, value in statuses.items())
            )
        if samples:
            parts.append("samples=" + "；".join(samples[:3]))
        return RunAIEvidenceItem(
            source="external_alerts",
            label="外部告警事件",
            detail="收到告警事件；" + "，".join(parts),
            target_section="monitor",
            severity="warn",
        )

    @staticmethod
    def _select_run_ai_top_metric_delta(
        verdict: RunVerdictResponse,
    ) -> Optional[RunVerdictMetricDelta]:
        meaningful: list[RunVerdictMetricDelta] = []
        for delta in verdict.metric_deltas:
            metric = str(delta.metric or "").strip()
            current = delta.current_value
            if metric == "error_rate" and current in (0, 0.0):
                continue
            if delta.baseline_value is not None:
                meaningful.append(delta)
                continue
            if delta.delta_ratio is not None and abs(float(delta.delta_ratio)) >= 0.01:
                meaningful.append(delta)
                continue
            if delta.delta_value is not None and abs(float(delta.delta_value)) > 0:
                meaningful.append(delta)

        if not meaningful:
            return None
        return max(meaningful, key=RunService._score_run_verdict_metric_delta)

    @staticmethod
    def _score_run_verdict_metric_delta(delta: RunVerdictMetricDelta) -> float:
        if delta.delta_ratio is not None:
            return abs(float(delta.delta_ratio))
        if delta.delta_value is not None:
            return abs(float(delta.delta_value))
        return 0.0

    @staticmethod
    def _format_run_verdict_metric_delta(delta: RunVerdictMetricDelta) -> str:
        parts = [delta.metric]
        if delta.current_value is not None:
            parts.append(
                f"当前={delta.current_value:.2f}{f' {delta.unit}' if delta.unit else ''}"
            )
        if delta.baseline_value is not None:
            parts.append(
                f"基线={delta.baseline_value:.2f}{f' {delta.unit}' if delta.unit else ''}"
            )
        if delta.delta_ratio is not None:
            parts.append(f"波动={(delta.delta_ratio * 100):+.1f}%")
        elif delta.delta_value is not None:
            parts.append(
                f"偏移={delta.delta_value:+.2f}{f' {delta.unit}' if delta.unit else ''}"
            )
        return " / ".join(parts)

    def _build_run_ai_primary_focus(
        self,
        *,
        run: Run,
        top_delta: Optional[RunVerdictMetricDelta],
        primary_dashboard: Optional[RunDashboardLink],
        baseline: Optional[RunBaselineResponse],
    ) -> Optional[RunAIPrimaryFocus]:
        runtime_reason = run.stop_reason or run.run_status_detail
        if runtime_reason:
            return RunAIPrimaryFocus(
                kind="runtime_logs",
                label="先看运行日志详情",
                detail=f"优先确认 {runtime_reason} 是否来自 runtime / agent 环境异常。",
                target_section="runtime_logs",
            )

        if primary_dashboard is not None:
            detail = "优先确认异常是否在外部监控面同步出现。"
            if top_delta is not None:
                detail = (
                    f"重点确认 {self._format_run_verdict_metric_delta(top_delta)} "
                    "是否在监控面同步出现。"
                )
            return RunAIPrimaryFocus(
                kind="dashboard",
                label=f"先打开「{primary_dashboard.title}」",
                detail=detail,
                url=primary_dashboard.url,
                dashboard_type=primary_dashboard.dashboard_type,
                target_section="monitor",
                metric=top_delta.metric if top_delta is not None else None,
            )

        if top_delta is not None:
            return RunAIPrimaryFocus(
                kind="summary_metrics",
                label=f"先看 {top_delta.metric} 指标差异",
                detail=self._format_run_verdict_metric_delta(top_delta),
                target_section="summary_metrics",
                metric=top_delta.metric,
            )

        if baseline is None:
            return RunAIPrimaryFocus(
                kind="baseline",
                label="先补当前作用域 baseline",
                detail="当前缺少 baseline，建议先在基线区设为当前基线，再继续判断长期波动。",
                target_section="baseline",
            )

        return None

    @staticmethod
    def _aggregate_summary_metric_rows(
        items: list[RunSummaryMetricRow],
    ) -> tuple[
        Optional[int],
        Optional[float],
        Optional[float],
        Optional[float],
        Optional[float],
        int,
    ]:
        if not items:
            return None, None, None, None, None, 0

        total_requests = sum(item.total_requests or 0 for item in items)
        total_throughput = sum(item.throughput or 0 for item in items)
        endpoint_total = len(items)

        def weighted_average(field: str) -> Optional[float]:
            rows = [
                item
                for item in items
                if isinstance(getattr(item, field, None), (int, float))
            ]
            if not rows:
                return None
            weighted_rows = [
                item
                for item in rows
                if isinstance(item.total_requests, int) and item.total_requests > 0
            ]
            if weighted_rows:
                weight_total = sum(item.total_requests or 0 for item in weighted_rows)
                if weight_total > 0:
                    return (
                        sum(
                            (getattr(item, field) or 0) * (item.total_requests or 0)
                            for item in weighted_rows
                        )
                        / weight_total
                    )
            return sum(getattr(item, field) or 0 for item in rows) / len(rows)

        return (
            total_requests if total_requests > 0 else None,
            total_throughput if total_throughput > 0 else None,
            weighted_average("avg_rt_ms"),
            weighted_average("p95_rt_ms"),
            weighted_average("p99_rt_ms"),
            endpoint_total,
        )

    def _resolve_k6_overview_contract_totals(
        self,
        run: Run,
    ) -> tuple[Optional[int], Optional[float]]:
        if not self._is_k6_engine(run) or not self._has_real_metric_context(run):
            return None, None

        (
            per_endpoint_total_requests,
            per_endpoint_throughput,
            _,
        ) = self._fetch_prometheus_live_k6_endpoint_contract_fields(
            run, step_seconds=10
        )

        total_requests = (
            sum(
                value
                for value in per_endpoint_total_requests.values()
                if isinstance(value, int) and value > 0
            )
            or None
        )
        throughput = (
            round(
                sum(
                    float(value)
                    for value in per_endpoint_throughput.values()
                    if isinstance(value, (int, float)) and float(value) > 0
                ),
                4,
            )
            if per_endpoint_throughput
            else None
        )
        return total_requests, throughput

    @staticmethod
    def _aggregate_checks_success_rate(items: list[RunCheckRow]) -> Optional[float]:
        values = [
            row.success_rate
            for row in items
            if isinstance(row.success_rate, (int, float))
        ]
        if not values:
            return None
        return sum(values) / len(values)

    def _build_compare_overview_summary(
        self,
        detail: Run,
        summary: RunSummaryMetricsResponse,
        checks: RunChecksResponse,
    ) -> RunOverviewSummary:
        total_requests, throughput, avg_rt_ms, p95_rt_ms, _p99_rt_ms, endpoint_total = (
            self._aggregate_summary_metric_rows(summary.items)
        )
        checks_success_rate = self._aggregate_checks_success_rate(checks.items)
        error_rate = detail.error_rate
        if error_rate is None and detail.success_rate is not None:
            error_rate = max(0.0, min(1.0, 1 - detail.success_rate))

        return RunOverviewSummary(
            total_requests=(
                detail.total_requests
                if detail.total_requests is not None
                else total_requests
            ),
            throughput=detail.rps if detail.rps is not None else throughput,
            avg_rt_ms=detail.avg_rt_ms if detail.avg_rt_ms is not None else avg_rt_ms,
            p95_rt_ms=detail.p95_rt_ms if detail.p95_rt_ms is not None else p95_rt_ms,
            error_rate=error_rate,
            checks_success_rate=(
                checks_success_rate
                if checks_success_rate is not None
                else detail.success_rate
            ),
            endpoint_total=endpoint_total if endpoint_total > 0 else None,
            summary_metrics_label=self._format_summary_metrics_label(
                total_requests=total_requests,
                throughput=throughput,
                endpoint_total=endpoint_total if endpoint_total > 0 else None,
            ),
            checks_summary_label=self._format_checks_summary_label(
                checks_success_rate,
                len(checks.items) if checks.items else None,
            ),
        )

    def get_run_compare(self, base_id: int, comparator_id: int) -> RunCompareResponse:
        def build_bundle(run_id: int) -> RunCompareBundle:
            detail = self.get_run(run_id)
            if detail is None:
                raise ValueError(f"Run {run_id} not found")
            summary = self.get_summary_metrics(run_id)
            checks = self.get_checks(run_id)
            detail.overview_summary = self._build_compare_overview_summary(
                detail,
                summary,
                checks,
            )
            endpoint_trends = {}
            for metric in (
                EndpointTrendMetric.THROUGHPUT,
                EndpointTrendMetric.RT_AVG_MS,
                EndpointTrendMetric.RT_P95_MS,
                EndpointTrendMetric.RT_P99_MS,
                EndpointTrendMetric.ERROR_RATE,
            ):
                endpoint_trends[metric.value] = self.get_endpoint_trends(
                    run_id=run_id,
                    metric=metric,
                    step_seconds=5,
                )
            return RunCompareBundle(
                detail=detail,
                overview_summary=self._build_compare_overview_summary(
                    detail,
                    summary,
                    checks,
                ),
                baseline=self.get_run_baseline(run_id),
                summary=summary,
                checks=checks,
                metrics=self.get_metrics(run_id, step_seconds=5),
                dashboards=self.get_dashboards(run_id),
                endpoint_trends=endpoint_trends,
            )

        return RunCompareResponse(
            base_id=base_id,
            comparator_id=comparator_id,
            base=build_bundle(base_id),
            comparator=build_bundle(comparator_id),
        )

    def stop_run(
        self, run_id: int, reason: Optional[str], user_id: Optional[int] = None
    ) -> Optional[Run]:
        run = self.repo.find_by_id(run_id)
        if not run:
            return None
        self._ensure_run_owner(run, user_id)
        if run.run_status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.STOPPED}:
            return run
        return self._finalize_active_run(
            run,
            final_status=RunStatus.STOPPED,
            reason=reason,
            status_detail=None,
            cleanup_remote=True,
            cleanup_k8s=False,
        )

    def govern_timed_out_run(
        self,
        run_id: int,
        *,
        reason: str = "timeout",
        status_detail: str = "timeout_watchdog",
    ) -> Optional[Run]:
        run = self.repo.find_by_id(run_id)
        if not run:
            return None
        if run.run_status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.STOPPED}:
            return run
        return self._finalize_active_run(
            run,
            final_status=RunStatus.FAILED,
            reason=reason,
            status_detail=status_detail,
            cleanup_remote=True,
            cleanup_k8s=True,
        )

    def stop_active_runs(
        self,
        *,
        reason: Optional[str],
        envs: Optional[list[str]] = None,
        user_id: Optional[int] = None,
    ) -> list[Run]:
        return self.stop_active_runs_bulk(
            reason=reason,
            envs=envs,
            user_id=user_id,
        ).stopped_runs

    def stop_active_runs_bulk(
        self,
        *,
        reason: Optional[str],
        envs: Optional[list[str]] = None,
        user_id: Optional[int] = None,
    ) -> BulkStopActiveRunsResult:
        normalized_envs = self._normalize_env_filters(envs)
        active_runs = list(self.repo.find_active_runs(envs=normalized_envs))
        for run in active_runs:
            self._ensure_run_owner(run, user_id)

        remote_summary = self._best_effort_stop_remote_runs_bulk(active_runs)
        stopped_runs: list[Run] = []
        for run in active_runs:
            stopped = self._finalize_active_run(
                run,
                final_status=RunStatus.STOPPED,
                reason=reason,
                status_detail=None,
                cleanup_remote=False,
                cleanup_k8s=False,
            )
            if stopped is not None:
                stopped_runs.append(stopped)
        return BulkStopActiveRunsResult(
            stopped_runs=stopped_runs,
            remote_stop_summary=remote_summary,
        )

    def _finalize_active_run(
        self,
        run: Run,
        *,
        final_status: RunStatus,
        reason: Optional[str],
        status_detail: Optional[str],
        cleanup_remote: bool,
        cleanup_k8s: bool,
    ) -> Run:
        if cleanup_remote:
            self._best_effort_stop_remote_run(run)

        run.run_status = final_status
        run.run_status_detail = status_detail
        run.stop_reason = reason
        end_ts = datetime.now(timezone.utc)
        run.ended_at = end_ts
        start_ts = self._as_utc(run.started_at)
        if start_ts:
            run.duration_seconds = int((end_ts - start_ts).total_seconds())
        if final_status == RunStatus.STOPPED:
            self._attach_k8s_cluster_kill_preview(run)
        updated = self.repo.update(run)

        if cleanup_k8s:
            k8s_meta = (updated.params or {}).get("k8s_job")
            if k8s_meta:
                try:
                    from app.core.agent_orchestrator import orchestrator

                    orchestrator.cleanup_k8s_job(k8s_meta)
                except Exception as exc:  # pragma: no cover - 容错兜底
                    logger.warning(
                        "cleanup k8s job failed for timed out run %s: %s",
                        updated.run_id,
                        exc,
                    )
        return updated

    def _attach_k8s_cluster_kill_preview(self, run: Run) -> None:
        params = run.params if isinstance(run.params, dict) else None
        if not params:
            return
        k8s_metas = self._collect_k8s_job_metas(params)
        if not k8s_metas:
            return

        items: list[dict] = []
        try:
            from app.core.agent_orchestrator import orchestrator
        except Exception as exc:  # pragma: no cover - import/runtime fallback
            logger.warning(
                "build k8s cluster kill preview failed for run %s: %s",
                run.run_id,
                exc,
            )
            return

        for meta in k8s_metas:
            try:
                dry_run = orchestrator.build_k8s_cluster_kill_dry_run(meta)
            except Exception as exc:  # pragma: no cover - preview is best effort
                logger.warning(
                    "build k8s cluster kill preview failed for run %s: %s",
                    run.run_id,
                    exc,
                )
                continue
            if isinstance(dry_run, dict):
                items.append(dry_run)

        if not items:
            return
        updated_params = dict(params)
        updated_params["k8s_cluster_kill_preview"] = {
            "dry_run": True,
            "preview_total": len(items),
            "items": items,
        }
        run.params = updated_params

    @staticmethod
    def _collect_k8s_job_metas(params: dict) -> list[dict]:
        metas: list[dict] = []
        root_meta = params.get("k8s_job")
        if isinstance(root_meta, dict):
            metas.append(root_meta)
        agent_runs = params.get("agent_runs")
        if isinstance(agent_runs, list):
            for item in agent_runs:
                if not isinstance(item, dict):
                    continue
                meta = item.get("k8s_job")
                if isinstance(meta, dict):
                    metas.append(meta)

        seen: set[str] = set()
        deduped: list[dict] = []
        for meta in metas:
            namespace = str(meta.get("namespace") or "")
            job_name = str(meta.get("job_name") or "")
            dedupe_key = f"{namespace}:{job_name}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            deduped.append(meta)
        return deduped

    @staticmethod
    def _normalize_env_filters(envs: Optional[list[str]]) -> Optional[list[str]]:
        if not envs:
            return None
        normalized: list[str] = []
        seen: set[str] = set()
        for item in envs:
            if not isinstance(item, str):
                continue
            value = item.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return normalized or None

    @staticmethod
    def _split_control_integer(
        total: Optional[int], bucket_count: int
    ) -> list[Optional[int]]:
        if total is None:
            return [None] * max(0, bucket_count)
        normalized_total = max(1, int(total))
        normalized_bucket_count = max(1, int(bucket_count))
        base = normalized_total // normalized_bucket_count
        remainder = normalized_total % normalized_bucket_count
        values: list[int] = []
        for index in range(normalized_bucket_count):
            value = base + (1 if index < remainder else 0)
            values.append(max(1, value))
        return values

    @staticmethod
    def _split_control_float(
        total: Optional[float], bucket_count: int
    ) -> list[Optional[float]]:
        if total is None:
            return [None] * max(0, bucket_count)
        normalized_total = max(0.0001, float(total))
        normalized_bucket_count = max(1, int(bucket_count))
        base_value = round(normalized_total / normalized_bucket_count, 4)
        values = [base_value for _ in range(normalized_bucket_count)]
        drift = round(normalized_total - sum(values), 4)
        if values:
            values[-1] = round(values[-1] + drift, 4)
        return [max(0.0001, value) for value in values]

    @staticmethod
    def _coerce_k6_control_agent_state(
        agent_host: str, payload: Optional[dict[str, Any]]
    ) -> RunK6ControlAgentState:
        if not isinstance(payload, dict):
            return RunK6ControlAgentState(
                agent_host=agent_host,
                available=False,
                reason="agent_control_unreachable",
            )
        return RunK6ControlAgentState(
            agent_host=agent_host,
            available=bool(payload.get("available")),
            reason=payload.get("reason"),
            supports_target_tps=bool(payload.get("supports_target_tps")),
            observed_tps=payload.get("observed_tps"),
            active_vus=payload.get("active_vus"),
            scenario_pre_allocated_vus=payload.get("scenario_pre_allocated_vus"),
            scenario_max_vus=payload.get("scenario_max_vus"),
            current_vus=payload.get("current_vus"),
            current_max_vus=payload.get("current_max_vus"),
            target_tps=payload.get("target_tps"),
            controller_enabled=bool(payload.get("controller_enabled")),
            controller_status=payload.get("controller_status"),
            controller_message=payload.get("controller_message"),
            metric_family=payload.get("metric_family"),
            last_synced_at=(
                datetime.fromisoformat(
                    str(payload.get("last_synced_at")).replace("Z", "+00:00")
                )
                if payload.get("last_synced_at")
                else None
            ),
            control_strategy=payload.get("control_strategy"),
            preferred_control_path=payload.get("preferred_control_path"),
            active_control_path=payload.get("active_control_path"),
            scenario_patch_supported=bool(payload.get("scenario_patch_supported")),
            scenario_patch_reason=payload.get("scenario_patch_reason"),
            script_family=payload.get("script_family"),
            scenario_configs=[
                RunK6ScenarioConfig(**item)
                for item in (payload.get("scenario_configs") or [])
                if isinstance(item, dict)
            ],
        )

    @staticmethod
    def _parse_k6_scenario_time_unit_seconds(value: Optional[str]) -> Optional[float]:
        raw = str(value or "").strip().lower()
        if not raw:
            return None
        matched = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)", raw)
        if not matched:
            return None
        amount = float(matched.group(1))
        unit = matched.group(2)
        if amount <= 0:
            return None
        if unit == "ms":
            return amount / 1000.0
        if unit == "s":
            return amount
        if unit == "m":
            return amount * 60.0
        return amount * 3600.0

    @classmethod
    def _compute_k6_scenario_config_total_tps(
        cls, scenario_configs: list[RunK6ScenarioConfig]
    ) -> Optional[float]:
        total_tps = 0.0
        matched = False
        for item in scenario_configs or []:
            rate = getattr(item, "rate", None)
            if rate is None:
                continue
            time_unit_seconds = (
                cls._parse_k6_scenario_time_unit_seconds(
                    getattr(item, "time_unit", None)
                )
                or 1.0
            )
            total_tps += float(rate) / max(time_unit_seconds, 0.001)
            matched = True
        if not matched:
            return None
        return round(total_tps, 4)

    @classmethod
    def _resolve_k6_summary_target_tps_from_scenario_configs(
        cls, response: RunK6ControlResponse
    ) -> Optional[float]:
        totals = [
            total
            for total in (
                cls._compute_k6_scenario_config_total_tps(agent.scenario_configs or [])
                for agent in (response.agents or [])
            )
            if total is not None and total > 0
        ]
        if not totals:
            return None
        return round(sum(totals), 4)

    @staticmethod
    def _resolve_scenario_direct_summary_false_reject_success_ratio() -> float:
        raw = os.getenv("PTP_SCENARIO_DIRECT_SUMMARY_FALSE_REJECT_SUCCESS_RATIO", "0.9")
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 0.9

    @classmethod
    def _reconcile_scenario_direct_summary_false_reject(
        cls,
        response: RunK6ControlResponse,
        *,
        contract_throughput: Optional[float],
    ) -> None:
        summary = response.summary
        if str(summary.control_strategy or "").strip().lower() != "scenario_direct":
            return
        if summary.target_tps is None:
            summary.target_tps = (
                cls._resolve_k6_summary_target_tps_from_scenario_configs(response)
            )
        if summary.target_tps is None or contract_throughput is None:
            return
        if str(summary.controller_status or "").strip().lower() != "rejected":
            return
        if _SCENARIO_DIRECT_RUNTIME_NOT_APPLIED_DETAIL not in str(
            summary.controller_message or ""
        ):
            return
        success_ratio = (
            cls._resolve_scenario_direct_summary_false_reject_success_ratio()
        )
        if float(contract_throughput) < float(summary.target_tps) * success_ratio:
            return
        summary.controller_enabled = True
        summary.controller_status = "applied"
        summary.controller_message = (
            f"scenario_direct target_tps={round(float(summary.target_tps), 4)}"
        )

    @staticmethod
    def _resolve_k6_control_update_failure_reason(
        agent_states: list[RunK6ControlAgentState],
        warnings: list[str],
    ) -> str:
        distinct_reasons = {
            str(agent.reason).strip()
            for agent in agent_states
            if not agent.available and agent.reason and str(agent.reason).strip()
        }
        if len(distinct_reasons) == 1:
            return next(iter(distinct_reasons))
        return "k6_control_update_failed: " + "; ".join(
            warnings or ["unknown_control_error"]
        )

    @staticmethod
    def _build_run_k6_control_response(
        run_id: int,
        agents: list[RunK6ControlAgentState],
        warnings: Optional[list[str]] = None,
    ) -> RunK6ControlResponse:
        warnings = warnings or []
        agent_total = len(agents)
        controllable_agents = [agent for agent in agents if agent.available]
        controllable_total = len(controllable_agents)
        all_agents_available = agent_total > 0 and controllable_total == agent_total
        supports_target_tps = all_agents_available and all(
            agent.supports_target_tps for agent in controllable_agents
        )
        observed_tps = (
            round(
                sum(float(agent.observed_tps or 0.0) for agent in controllable_agents),
                4,
            )
            if controllable_agents
            else None
        )
        active_vus_values = [
            int(agent.active_vus)
            for agent in controllable_agents
            if agent.active_vus is not None
        ]
        active_vus = sum(active_vus_values) if active_vus_values else None
        scenario_pre_allocated_vus_values = [
            int(agent.scenario_pre_allocated_vus)
            for agent in controllable_agents
            if agent.scenario_pre_allocated_vus is not None
        ]
        scenario_pre_allocated_vus = (
            sum(scenario_pre_allocated_vus_values)
            if scenario_pre_allocated_vus_values
            else None
        )
        scenario_max_vus_values = [
            int(agent.scenario_max_vus)
            for agent in controllable_agents
            if agent.scenario_max_vus is not None
        ]
        scenario_max_vus = (
            sum(scenario_max_vus_values) if scenario_max_vus_values else None
        )
        current_vus = (
            sum(int(agent.current_vus or 0) for agent in controllable_agents)
            if controllable_agents
            else None
        )
        current_max_vus = (
            sum(int(agent.current_max_vus or 0) for agent in controllable_agents)
            if controllable_agents
            else None
        )
        target_tps_values = [
            round(float(agent.target_tps), 4)
            for agent in controllable_agents
            if agent.target_tps is not None
        ]
        target_tps = None
        if target_tps_values:
            target_tps = (
                target_tps_values[0]
                if max(target_tps_values) - min(target_tps_values) <= 1e-6
                else max(target_tps_values)
            )
        last_synced_at = None
        for agent in controllable_agents:
            if agent.last_synced_at is None:
                continue
            if last_synced_at is None or agent.last_synced_at > last_synced_at:
                last_synced_at = agent.last_synced_at
        controller_enabled = any(
            agent.controller_enabled for agent in controllable_agents
        )
        controller_status = next(
            (
                agent.controller_status
                for agent in controllable_agents
                if agent.controller_status
            ),
            None,
        )
        controller_message = next(
            (
                agent.controller_message
                for agent in controllable_agents
                if agent.controller_message
            ),
            None,
        )
        control_strategy = next(
            (
                agent.control_strategy
                for agent in controllable_agents
                if agent.control_strategy
            ),
            None,
        )
        preferred_control_path = next(
            (
                agent.preferred_control_path
                for agent in controllable_agents
                if agent.preferred_control_path
            ),
            None,
        )
        active_control_path = next(
            (
                agent.active_control_path
                for agent in controllable_agents
                if agent.active_control_path
            ),
            None,
        )
        script_family = next(
            (
                agent.script_family
                for agent in controllable_agents
                if agent.script_family
            ),
            None,
        )
        scenario_patch_supported = all_agents_available and all(
            agent.scenario_patch_supported for agent in controllable_agents
        )
        scenario_patch_reason = next(
            (
                agent.scenario_patch_reason
                for agent in controllable_agents
                if agent.scenario_patch_reason
            ),
            None,
        )
        summary = RunK6ControlSummary(
            agent_total=agent_total,
            controllable_agent_total=controllable_total,
            supports_target_tps=supports_target_tps,
            observed_tps=observed_tps,
            active_vus=active_vus,
            scenario_pre_allocated_vus=scenario_pre_allocated_vus,
            scenario_max_vus=scenario_max_vus,
            current_vus=current_vus,
            current_max_vus=current_max_vus,
            target_tps=target_tps,
            controller_enabled=controller_enabled,
            controller_status=controller_status,
            controller_message=controller_message,
            last_synced_at=last_synced_at,
            control_strategy=control_strategy,
            preferred_control_path=preferred_control_path,
            active_control_path=active_control_path,
            scenario_patch_supported=scenario_patch_supported,
            scenario_patch_reason=scenario_patch_reason,
            script_family=script_family,
        )
        unavailable_reason = None
        if controllable_total <= 0:
            distinct_reasons = {
                str(agent.reason).strip()
                for agent in agents
                if agent.reason and str(agent.reason).strip()
            }
            if len(distinct_reasons) == 1:
                unavailable_reason = next(iter(distinct_reasons))
            else:
                unavailable_reason = "no_controllable_agents"
        return RunK6ControlResponse(
            run_id=run_id,
            engine_type="k6",
            available=controllable_total > 0,
            reason=None if controllable_total > 0 else unavailable_reason,
            summary=summary,
            agents=agents,
            warnings=warnings,
        )

    def get_k6_control(
        self, run_id: int, user_id: Optional[int] = None
    ) -> RunK6ControlResponse:
        run = self.repo.find_by_id(run_id)
        if not run:
            raise ValueError("Run not found")
        self._sync_terminal_run_from_agent_status(run)
        self._ensure_run_owner(run, user_id)
        if not self._is_k6_engine(run):
            raise RuntimeError("run is not k6")
        agent_contexts = self._get_agent_contexts(run)
        if not agent_contexts:
            raise RuntimeError("run has no agent control context")

        from app.core.agent_orchestrator import orchestrator

        agent_states: list[RunK6ControlAgentState] = []
        warnings: list[str] = []
        for agent_host, run_token in agent_contexts:
            payload = self._run_async(
                orchestrator.fetch_run_k6_control(agent_host, run_token)
            )
            state = self._coerce_k6_control_agent_state(agent_host, payload)
            if not state.available:
                warnings.append(
                    f"{agent_host}: {state.reason or 'agent_control_unreachable'}"
                )
            agent_states.append(state)
        response = self._build_run_k6_control_response(run_id, agent_states, warnings)
        if run.run_status == RunStatus.RUNNING:
            _, contract_throughput = self._resolve_k6_overview_contract_totals(run)
            if contract_throughput is not None:
                response.summary.observed_tps = contract_throughput
            self._reconcile_scenario_direct_summary_false_reject(
                response,
                contract_throughput=contract_throughput,
            )
        return response

    def update_k6_control(
        self,
        run_id: int,
        request: RunK6ControlRequest,
        user_id: Optional[int] = None,
    ) -> RunK6ControlResponse:
        run = self.repo.find_by_id(run_id)
        if not run:
            raise ValueError("Run not found")
        self._ensure_run_owner(run, user_id)
        if not self._is_k6_engine(run):
            raise RuntimeError("run is not k6")
        if run.run_status != RunStatus.RUNNING:
            raise RuntimeError("run is not running")
        agent_contexts = self._get_agent_contexts(run)
        if not agent_contexts:
            raise RuntimeError("run has no agent control context")

        current = self.get_k6_control(run_id, user_id=user_id)
        if current.summary.controllable_agent_total != current.summary.agent_total:
            raise RuntimeError("not_all_agents_support_k6_control")
        if request.target_tps is None:
            raise RuntimeError("target_tps_required")

        from app.core.agent_orchestrator import orchestrator

        agent_states: list[RunK6ControlAgentState] = []
        warnings: list[str] = []
        for agent_host, run_token in agent_contexts:
            payload: dict[str, Any] = {"target_tps": request.target_tps}
            result = self._run_async(
                orchestrator.update_run_k6_control(agent_host, run_token, payload)
            )
            state = self._coerce_k6_control_agent_state(agent_host, result)
            if not state.available:
                warnings.append(
                    f"{agent_host}: {state.reason or 'agent_control_update_failed'}"
                )
            agent_states.append(state)

        if any(not agent.available for agent in agent_states):
            raise RuntimeError(
                self._resolve_k6_control_update_failure_reason(agent_states, warnings)
            )

        return self._build_run_k6_control_response(run_id, agent_states, warnings)

    def submit_run_k6_control_job(
        self,
        *,
        run_id: int,
        request: RunK6ControlRequest,
        user_id: Optional[int] = None,
    ) -> RunK6ControlAcceptedResponse:
        run = self.repo.find_by_id(run_id)
        if not run:
            raise ValueError("Run not found")
        self._ensure_run_owner(run, user_id)
        if not self._is_k6_engine(run):
            raise RuntimeError("run is not k6")
        if run.run_status != RunStatus.RUNNING:
            raise RuntimeError("run is not running")
        if request.target_tps is None:
            raise RuntimeError("target_tps_required")

        current = self.get_k6_control(run_id, user_id=user_id)
        if current.summary.controllable_agent_total != current.summary.agent_total:
            raise RuntimeError("not_all_agents_support_k6_control")
        if not current.summary.supports_target_tps:
            raise RuntimeError("target_tps_not_supported")

        from app.core.celery_app import CONTROL_QUEUE
        from app.tasks.test_executor import execute_run_k6_control_task

        task = execute_run_k6_control_task.apply_async(
            kwargs={
                "run_id": run_id,
                "request_payload": request.model_dump(exclude_none=True),
                "user_id": user_id,
            },
            queue=CONTROL_QUEUE,
        )
        return RunK6ControlAcceptedResponse(
            run_id=run_id,
            accepted=True,
            async_task_id=str(task.id),
            target_tps=round(float(request.target_tps), 4),
        )

    def get_run_k6_control_job_status(
        self,
        *,
        run_id: int,
        task_id: str,
        user_id: Optional[int] = None,
    ) -> RunK6ControlTaskStatusResponse:
        run = self.repo.find_by_id(run_id)
        if not run:
            raise ValueError("Run not found")
        self._ensure_run_owner(run, user_id)

        from app.tasks.test_executor import build_run_k6_control_task_status

        return build_run_k6_control_task_status(run_id=run_id, task_id=task_id)

    def get_metrics(
        self,
        run_id: int,
        started_from: Optional[datetime] = None,
        started_to: Optional[datetime] = None,
        metric: Optional[str] = None,
        step_seconds: int = 10,
    ) -> MetricsResponse:
        run = self.repo.find_by_id(run_id)
        if not run:
            return MetricsResponse(step_seconds=step_seconds, series=[])

        now = datetime.now(timezone.utc).replace(microsecond=0)
        start = self._as_utc(started_from) or self._as_utc(run.started_at) or now
        end = self._as_utc(started_to) or self._as_utc(run.ended_at) or now
        if start > end:
            start, end = end, start

        real_metrics = self._get_real_metrics_response(
            run,
            started_from=started_from,
            started_to=started_to,
            metric=metric,
            step_seconds=step_seconds,
        )
        if real_metrics:
            return self._append_error_rate_fallback_series(
                run=run,
                metrics=real_metrics,
                metric=metric,
                start=start,
                end=end,
                step_seconds=step_seconds,
            )

        total_seconds = int((end - start).total_seconds())
        steps = max(1, min(200, total_seconds // max(step_seconds, 1) + 1))

        rng = random.Random(run_id)
        base_rps = (
            float(rng.randint(50, 200))
            if run.run_status == RunStatus.RUNNING
            else float(rng.randint(5, 50))
        )
        base_rt = float(rng.randint(80, 250))
        base_err = float(rng.randint(0, 5)) / 100.0

        metric_whitelist = {
            MetricName.RPS.value: MetricName.RPS,
            MetricName.RT_AVG_MS.value: MetricName.RT_AVG_MS,
            MetricName.RT_P95_MS.value: MetricName.RT_P95_MS,
            MetricName.RT_P99_MS.value: MetricName.RT_P99_MS,
            MetricName.ERROR_RATE.value: MetricName.ERROR_RATE,
        }

        selected: list[MetricName]
        if metric:
            m = metric_whitelist.get(metric)
            selected = [m] if m else []
        else:
            selected = [MetricName.RPS, MetricName.RT_P95_MS]

        series: list[MetricsSeries] = []
        for metric_name in selected:
            points: list[MetricPoint] = []
            for i in range(steps):
                ts = start + (end - start) * (i / max(steps - 1, 1))
                ts = ts.replace(microsecond=0)
                drift = (
                    (i / max(steps - 1, 1))
                    if run.run_status == RunStatus.RUNNING
                    else 0.0
                )

                if metric_name == MetricName.RPS:
                    value = max(
                        0.0, base_rps * (0.8 + 0.4 * rng.random()) * (1.0 + 0.2 * drift)
                    )
                    unit = "rps"
                elif metric_name == MetricName.RT_AVG_MS:
                    value = max(0.0, base_rt * (0.9 + 0.2 * rng.random()))
                    unit = "ms"
                elif metric_name == MetricName.RT_P95_MS:
                    value = max(
                        0.0,
                        base_rt
                        * 1.4
                        * (0.9 + 0.3 * rng.random())
                        * (1.0 + 0.1 * drift),
                    )
                    unit = "ms"
                elif metric_name == MetricName.RT_P99_MS:
                    value = max(
                        0.0,
                        base_rt
                        * 1.8
                        * (0.9 + 0.4 * rng.random())
                        * (1.0 + 0.15 * drift),
                    )
                    unit = "ms"
                else:
                    continue

                points.append(MetricPoint(ts=ts, value=round(float(value), 4)))

            if points:
                series.append(
                    MetricsSeries(metric=metric_name, unit=unit, points=points)
                )

        return self._append_error_rate_fallback_series(
            run=run,
            metrics=MetricsResponse(step_seconds=step_seconds, series=series),
            metric=metric,
            start=start,
            end=end,
            step_seconds=step_seconds,
        )

    def _append_error_rate_fallback_series(
        self,
        *,
        run: Run,
        metrics: MetricsResponse,
        metric: Optional[str],
        start: datetime,
        end: datetime,
        step_seconds: int,
    ) -> MetricsResponse:
        if metric and metric != MetricName.ERROR_RATE.value:
            return metrics
        if any(series.metric == MetricName.ERROR_RATE for series in metrics.series):
            return metrics

        fallback_value = self._build_overall_summary_metric_fallback_values(run).get(
            "error_rate"
        )
        if fallback_value is None:
            return metrics

        timestamps = sorted(
            {
                point.ts
                for series in metrics.series
                for point in series.points
                if point.ts is not None
            }
        )
        if not timestamps:
            timestamps = [start, end] if start != end else [end]

        error_rate_series = MetricsSeries(
            metric=MetricName.ERROR_RATE,
            unit="ratio",
            points=[
                MetricPoint(ts=ts, value=float(fallback_value)) for ts in timestamps
            ],
        )
        return MetricsResponse(
            step_seconds=metrics.step_seconds or step_seconds,
            series=[*metrics.series, error_rate_series],
        )

    def _get_real_metrics_response(
        self,
        run: Run,
        started_from: Optional[datetime] = None,
        started_to: Optional[datetime] = None,
        metric: Optional[str] = None,
        step_seconds: int = 10,
    ) -> Optional[MetricsResponse]:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        start = self._as_utc(started_from) or self._as_utc(run.started_at) or now
        end = self._as_utc(started_to) or self._as_utc(run.ended_at) or now
        if start > end:
            start, end = end, start

        agent_contexts = self._get_agent_contexts(run)
        if len(agent_contexts) > 1:
            try:
                aggregated_agent_metrics = self._fetch_agent_metrics_from_all_contexts(
                    agent_contexts,
                    metric=metric,
                    step_seconds=step_seconds,
                )
                if aggregated_agent_metrics:
                    return aggregated_agent_metrics
            except Exception as exc:  # pragma: no cover - 容错兜底
                logger.warning(
                    "fallback to single-agent metrics for run %s due to aggregation error: %s",
                    run.run_id,
                    exc,
                )

        run_token = None
        if run.params:
            run_token = (
                run.params.get("agent_run_token")
                or run.params.get("agent_token")
                or run.params.get("agent_session")
            )

        # 优先使用 Prometheus（若提供），其次 PushGateway（终态）
        if run_token:
            prom_metrics = self._fetch_prometheus_metrics(
                run_token=run_token,
                start=start,
                end=end,
                step_seconds=step_seconds,
                metric=metric,
            )
            if prom_metrics:
                return prom_metrics
            pg_metrics = self._fetch_pushgateway_metrics(
                run_token=run_token, step_seconds=step_seconds, metric=metric
            )
            if pg_metrics:
                return pg_metrics

        agent_ctx = agent_contexts[0] if agent_contexts else None
        if agent_ctx:
            try:
                agent_metrics = self._fetch_agent_metrics(
                    agent_ctx, metric=metric, step_seconds=step_seconds
                )
                if agent_metrics:
                    return agent_metrics
            except Exception as exc:  # pragma: no cover - 容错兜底
                logger.warning(
                    "fallback to placeholder metrics for run %s due to agent error: %s",
                    run.run_id,
                    exc,
                )
        metrics_s3_uri = (run.params or {}).get("metrics_s3")
        if metrics_s3_uri:
            try:
                s3_metrics = self._fetch_s3_metrics(
                    metrics_s3_uri,
                    metric=metric,
                    step_seconds=step_seconds,
                    started_from=start,
                    started_to=end,
                )
                if s3_metrics:
                    return s3_metrics
            except Exception as exc:  # pragma: no cover - 容错
                logger.warning(
                    "fallback to placeholder metrics due to s3 error for run %s: %s",
                    run.run_id,
                    exc,
                )
        return None

    def get_logs(
        self,
        run_id: int,
        cursor: Optional[str] = None,
        limit: int = 200,
        view: str = "all",
        level: Optional[str] = None,
        source: Optional[str] = None,
        order: str = "asc",
    ) -> LogsResponse:
        run = self.repo.find_by_id(run_id)
        if not run:
            return LogsResponse(items=[], next_cursor=None)

        agent_contexts = self._get_agent_contexts(run)
        if view == "exception" and agent_contexts:
            agent_batches = self._collect_agent_log_batches(
                agent_contexts,
                view=view,
                level=level,
                source=source,
            )
            if agent_batches:
                filtered = self._normalize_log_batches(agent_batches)
                return self._paginate_log_items(
                    filtered, cursor=cursor, limit=limit, order=order
                )

        if agent_contexts:
            agent_logs = self._fetch_agent_logs_from_all_contexts(
                run_id,
                agent_contexts,
                cursor=cursor,
                limit=limit,
                order=order,
            )
            if agent_logs and agent_logs.items:
                agent_logs.items = self._filter_log_items(
                    agent_logs.items,
                    view=view,
                    level=level,
                    source=source,
                )
                return agent_logs

        # S3 日志归档兜底（优先 log_s3，其次 k8s_log_s3）
        params = run.params or {}
        if view == "exception":
            s3_batches = self._collect_s3_log_batches_from_run(
                run,
                view=view,
                level=level,
                source=source,
            )
            if s3_batches:
                filtered = self._normalize_log_batches(s3_batches)
                return self._paginate_log_items(
                    filtered, cursor=cursor, limit=limit, order=order
                )

        aggregated_s3_logs = self._fetch_s3_logs_from_all_uris(
            run, cursor=cursor, limit=limit, order=order
        )
        if aggregated_s3_logs and aggregated_s3_logs.items:
            aggregated_s3_logs.items = self._filter_log_items(
                aggregated_s3_logs.items,
                view=view,
                level=level,
                source=source,
            )
            return aggregated_s3_logs

        s3_log_uris = [params.get("log_s3"), params.get("k8s_log_s3")]
        seen_s3_uris: set[str] = set()
        for s3_uri in s3_log_uris:
            if not s3_uri or s3_uri in seen_s3_uris:
                continue
            seen_s3_uris.add(s3_uri)
            try:
                if view == "exception":
                    full_items = self._collect_s3_log_items(s3_uri)
                    s3_logs = self._paginate_log_items(
                        self._filter_log_items(
                            full_items, view=view, level=level, source=source
                        ),
                        cursor=cursor,
                        limit=limit,
                        order=order,
                    )
                else:
                    s3_logs = self._fetch_s3_logs(
                        s3_uri, cursor=cursor, limit=limit, order=order
                    )
                if s3_logs:
                    if view != "exception":
                        s3_logs.items = self._filter_log_items(
                            s3_logs.items,
                            view=view,
                            level=level,
                            source=source,
                        )
                    return s3_logs
            except Exception as exc:  # pragma: no cover - 容错
                logger.debug("fallback to placeholder logs due to s3 error: %s", exc)

        now = datetime.now(timezone.utc)
        # 若 agent 未返回，尝试 S3 归档
        for s3_uri in s3_log_uris:
            if not s3_uri:
                continue
            try:
                if view == "exception":
                    s3_logs = self._paginate_log_items(
                        self._filter_log_items(
                            self._collect_s3_log_items(s3_uri),
                            view=view,
                            level=level,
                            source=source,
                        ),
                        cursor=cursor,
                        limit=limit,
                        order=order,
                    )
                else:
                    s3_logs = self._fetch_s3_logs(
                        s3_uri, cursor=cursor, limit=limit, order=order
                    )
                if s3_logs and s3_logs.items:
                    if view != "exception":
                        s3_logs.items = self._filter_log_items(
                            s3_logs.items,
                            view=view,
                            level=level,
                            source=source,
                        )
                    return s3_logs
            except Exception as exc:  # pragma: no cover - 容错
                logger.warning("get_logs s3 failed for run %s: %s", run_id, exc)

        # 推断式 S3 归档：有 token 且启用 S3 时尝试按默认前缀读取
        fallback_agent_ctx = agent_contexts[0] if agent_contexts else None
        if fallback_agent_ctx and (
            os.getenv("LOG_ARCHIVE_S3", os.getenv("USE_S3", "0")) == "1"
        ):
            host, token = fallback_agent_ctx
            bucket = os.getenv("S3_BUCKET") or settings.S3_BUCKET
            prefix = get_run_artifact_prefix()
            if bucket and token:
                guessed_uri = f"s3://{bucket}/{prefix}/{token}.log"
                try:
                    if view == "exception":
                        s3_logs = self._paginate_log_items(
                            self._filter_log_items(
                                self._collect_s3_log_items(guessed_uri),
                                view=view,
                                level=level,
                                source=source,
                            ),
                            cursor=cursor,
                            limit=limit,
                            order=order,
                        )
                    else:
                        s3_logs = self._fetch_s3_logs(
                            guessed_uri, cursor=cursor, limit=limit, order=order
                        )
                    if s3_logs and s3_logs.items:
                        if view != "exception":
                            s3_logs.items = self._filter_log_items(
                                s3_logs.items,
                                view=view,
                                level=level,
                                source=source,
                            )
                        return s3_logs
                except Exception:
                    pass

        # 占位日志（保证契约返回）
        base_ts = (
            self._as_utc(run.started_at) or self._as_utc(run.created_at) or now
        ).replace(microsecond=0)
        all_items: list[LogItem] = []

        # deterministic timeline
        seq = 1
        all_items.append(
            LogItem(
                seq=seq,
                ts=base_ts,
                level="INFO",
                message="run_created",
                source="ptp-admin",
            )
        )
        seq += 1
        all_items.append(
            LogItem(
                seq=seq,
                ts=base_ts,
                level="INFO",
                message=f"run_status={run.run_status.value}",
                source="ptp-admin",
            )
        )
        seq += 1
        if run.run_status_detail:
            all_items.append(
                LogItem(
                    seq=seq,
                    ts=base_ts + timedelta(seconds=1),
                    level="INFO",
                    message=f"run_status_detail={run.run_status_detail}",
                    source="ptp-admin",
                )
            )
            seq += 1
        if run.stop_reason:
            all_items.append(
                LogItem(
                    seq=seq,
                    ts=base_ts + timedelta(seconds=2),
                    level="WARN",
                    message=f"stop_reason={run.stop_reason}",
                    source="ptp-admin",
                )
            )
            seq += 1

        all_items.append(
            LogItem(
                seq=seq,
                ts=base_ts + timedelta(seconds=3),
                level="INFO",
                message="logs_placeholder: not yet connected to agent stdout/stderr",
                source="ptp-admin",
            )
        )

        all_items = self._filter_log_items(
            all_items,
            view=view,
            level=level,
            source=source,
        )

        start_seq = self._decode_cursor(cursor) if cursor else 0

        if order == "desc":
            filtered = [i for i in all_items if i.seq < start_seq or start_seq == 0]
            filtered.sort(key=lambda x: x.seq, reverse=True)
        else:
            filtered = [i for i in all_items if i.seq > start_seq]
            filtered.sort(key=lambda x: x.seq)

        sliced = filtered[: max(1, min(limit, 2000))]
        next_cursor = self._encode_cursor(sliced[-1].seq) if sliced else None
        return LogsResponse(items=sliced, next_cursor=next_cursor)

    def _decode_cursor(self, cursor: str) -> int:
        try:
            raw = base64.urlsafe_b64decode(cursor.encode("utf-8") + b"==")
            obj = json.loads(raw.decode("utf-8"))
            return int(obj.get("seq", 0))
        except Exception:
            try:
                return int(cursor)
            except Exception:
                return 0

    def _encode_cursor(self, seq_value: int) -> str:
        raw = json.dumps({"seq": seq_value}, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")

    def _filter_log_items(
        self,
        items: list[LogItem],
        *,
        view: str = "all",
        level: Optional[str] = None,
        source: Optional[str] = None,
    ) -> list[LogItem]:
        filtered = items
        if view == "exception":
            exception_levels = {"warn", "warning", "error", "fatal", "critical"}
            filtered = [
                item for item in filtered if item.level.lower() in exception_levels
            ]
        if level:
            filtered = [
                item for item in filtered if item.level.lower() == level.lower()
            ]
        if source:
            filtered = [
                item
                for item in filtered
                if (item.source or "").lower() == source.lower()
            ]
        return filtered

    def _as_utc(self, dt: Optional[datetime]) -> Optional[datetime]:
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _get_agent_contexts(self, run: Run) -> list[tuple[str, str]]:
        params = getattr(run, "params", None) or {}
        contexts: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()

        agent_runs = params.get("agent_runs")
        if isinstance(agent_runs, list):
            for item in agent_runs:
                if not isinstance(item, dict):
                    continue
                host = item.get("agent_host") or item.get("agent_ip")
                token = (
                    item.get("agent_run_token")
                    or item.get("agent_token")
                    or item.get("agent_session")
                    or item.get("run_token")
                )
                if not isinstance(host, str) or not host:
                    continue
                if not isinstance(token, str) or not token:
                    continue
                ctx = (host, token)
                if ctx in seen:
                    continue
                seen.add(ctx)
                contexts.append(ctx)

        host = params.get("agent_host")
        token = (
            params.get("agent_run_token")
            or params.get("agent_token")
            or params.get("agent_session")
        )
        if isinstance(host, str) and host and isinstance(token, str) and token:
            ctx = (host, token)
            if ctx not in seen:
                contexts.append(ctx)

        return contexts

    def _get_agent_context(self, run: Run) -> Optional[tuple[str, str]]:
        contexts = self._get_agent_contexts(run)
        return contexts[0] if contexts else None

    def _iter_agent_run_entries(self, run: Run) -> list[dict[str, Any]]:
        params = getattr(run, "params", None) or {}
        agent_runs = params.get("agent_runs")
        if not isinstance(agent_runs, list):
            return []
        return [item for item in agent_runs if isinstance(item, dict)]

    def _collect_agent_run_field_values(self, run: Run, *field_names: str) -> list[str]:
        params = getattr(run, "params", None) or {}
        values: list[str] = []
        seen: set[str] = set()

        for field_name in field_names:
            value = params.get(field_name)
            if isinstance(value, str) and value and value not in seen:
                seen.add(value)
                values.append(value)

        for entry in self._iter_agent_run_entries(run):
            for field_name in field_names:
                value = entry.get(field_name)
                if isinstance(value, str) and value and value not in seen:
                    seen.add(value)
                    values.append(value)

        return values

    def _has_real_metric_context(self, run: Run) -> bool:
        params = getattr(run, "params", None) or {}
        return bool(
            params.get("agent_run_token")
            or params.get("agent_token")
            or params.get("agent_session")
            or params.get("metrics_s3")
            or self._get_agent_contexts(run)
        )

    @staticmethod
    def _is_k6_engine(run: Run) -> bool:
        engine_type = getattr(run, "engine_type", None)
        raw_value = getattr(engine_type, "value", engine_type)
        return str(raw_value or "").strip().lower() == "k6"

    @staticmethod
    def _is_jmeter_engine(run: Run) -> bool:
        engine_type = getattr(run, "engine_type", None)
        raw_value = getattr(engine_type, "value", engine_type)
        return str(raw_value or "").strip().lower() == "jmeter"

    def _best_effort_stop_remote_run(self, run: Run) -> None:
        agent_contexts = self._get_agent_contexts(run)
        if not agent_contexts:
            return
        for agent_host, run_token in agent_contexts:
            try:
                from app.core.agent_orchestrator import orchestrator

                result = self._run_async(orchestrator.stop_run(agent_host, run_token))
                if result:
                    logger.info(
                        "remote stop sent: run_id=%s host=%s result=%s",
                        run.run_id,
                        agent_host,
                        result,
                    )
            except Exception as exc:  # pragma: no cover - 容错兜底
                logger.warning(
                    "remote stop failed for run %s host=%s token=%s: %s",
                    run.run_id,
                    agent_host,
                    run_token,
                    exc,
                )

    def _best_effort_stop_remote_runs_bulk(self, runs: list[Run]) -> dict[str, Any]:
        contexts: list[dict[str, Any]] = []
        seen: set[tuple[int, str, str]] = set()
        skipped_no_context = 0
        for run in runs:
            run_contexts = self._get_agent_contexts(run)
            if not run_contexts:
                skipped_no_context += 1
                continue
            for agent_host, run_token in run_contexts:
                key = (int(run.run_id), agent_host, run_token)
                if key in seen:
                    continue
                seen.add(key)
                contexts.append(
                    {
                        "run_id": int(run.run_id),
                        "agent_host": agent_host,
                        "run_token": run_token,
                    }
                )

        timeout_seconds = self._bulk_remote_stop_timeout_seconds()
        concurrency = self._bulk_remote_stop_concurrency()
        summary: dict[str, Any] = {
            "attempted": len(contexts),
            "succeeded": 0,
            "failed": 0,
            "timed_out": 0,
            "skipped_no_context": skipped_no_context,
            "timeout_seconds": timeout_seconds,
            "concurrency": concurrency,
            "errors": [],
        }
        if not contexts:
            return summary

        try:
            result = self._run_async(
                self._stop_remote_contexts_concurrently(
                    contexts,
                    timeout_seconds=timeout_seconds,
                    concurrency=concurrency,
                )
            )
        except Exception as exc:  # pragma: no cover - bulk stop is best-effort
            logger.warning("bulk remote stop failed before completion: %s", exc)
            summary["failed"] = len(contexts)
            summary["errors"] = [
                {
                    "run_id": item["run_id"],
                    "agent_host": item["agent_host"],
                    "error": str(exc),
                }
                for item in contexts[:5]
            ]
            return summary

        summary.update(result)
        return summary

    async def _stop_remote_contexts_concurrently(
        self,
        contexts: list[dict[str, Any]],
        *,
        timeout_seconds: float,
        concurrency: int,
    ) -> dict[str, Any]:
        from app.core.agent_orchestrator import orchestrator

        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def stop_one(item: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                run_id = item["run_id"]
                agent_host = item["agent_host"]
                run_token = item["run_token"]
                try:
                    result = await asyncio.wait_for(
                        orchestrator.stop_run(agent_host, run_token),
                        timeout=timeout_seconds,
                    )
                    if result is None:
                        logger.warning(
                            "bulk remote stop returned empty result for run %s host=%s token=%s",
                            run_id,
                            agent_host,
                            run_token,
                        )
                        return {
                            "status": "failed",
                            "error": "empty_remote_stop_result",
                            **item,
                        }
                    logger.info(
                        "bulk remote stop sent: run_id=%s host=%s result=%s",
                        run_id,
                        agent_host,
                        result,
                    )
                    return {"status": "succeeded", **item}
                except asyncio.TimeoutError:
                    logger.warning(
                        "bulk remote stop timed out for run %s host=%s token=%s",
                        run_id,
                        agent_host,
                        run_token,
                    )
                    return {"status": "timed_out", **item}
                except Exception as exc:  # pragma: no cover - best-effort fallback
                    logger.warning(
                        "bulk remote stop failed for run %s host=%s token=%s: %s",
                        run_id,
                        agent_host,
                        run_token,
                        exc,
                    )
                    return {"status": "failed", "error": str(exc), **item}

        results = await asyncio.gather(*(stop_one(item) for item in contexts))
        errors = [
            {
                "run_id": item["run_id"],
                "agent_host": item["agent_host"],
                "status": item["status"],
                **({"error": item["error"]} if item.get("error") else {}),
            }
            for item in results
            if item["status"] != "succeeded"
        ][:10]
        return {
            "attempted": len(contexts),
            "succeeded": sum(1 for item in results if item["status"] == "succeeded"),
            "failed": sum(1 for item in results if item["status"] == "failed"),
            "timed_out": sum(1 for item in results if item["status"] == "timed_out"),
            "errors": errors,
        }

    @staticmethod
    def _bulk_remote_stop_timeout_seconds() -> float:
        raw = os.getenv("AGENT_BULK_STOP_REMOTE_TIMEOUT_SECONDS", "5")
        try:
            parsed = float(str(raw).strip())
        except (TypeError, ValueError):
            parsed = 5.0
        return max(0.1, parsed)

    @staticmethod
    def _bulk_remote_stop_concurrency() -> int:
        raw = os.getenv("AGENT_BULK_STOP_REMOTE_CONCURRENCY", "50")
        try:
            parsed = int(str(raw).strip())
        except (TypeError, ValueError):
            parsed = 50
        return max(1, min(parsed, 200))

    def _ensure_run_owner(self, run: Run, user_id: Optional[int]) -> None:
        if user_id is None:
            return
        task = self.db.query(Task).filter(Task.id == run.task_id).first()
        if (
            task
            and task.created_by
            and int(task.created_by) != int(user_id)
            and not self._is_task_access_exempt_user(user_id)
        ):
            raise PermissionError("Forbidden: owner only")

    def _is_task_access_exempt_user(self, user_id: int) -> bool:
        try:
            parsed_user_id = int(user_id)
        except (TypeError, ValueError):
            return False

        user = self.db.get(User, parsed_user_id)
        if user is None:
            return False
        if bool(user.is_superuser):
            return True

        role_value = (
            user.role.value
            if isinstance(user.role, UserRole)
            else str(user.role).strip().upper()
        )
        return role_value == UserRole.ADMIN.value

    def _run_async(self, coroutine):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        result_holder = {"result": None, "error": None}

        def _runner():
            try:
                result_holder["result"] = asyncio.run(coroutine)
            except Exception as exc:  # pragma: no cover - 容错
                result_holder["error"] = exc

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join()
        if result_holder["error"] is not None:
            raise result_holder["error"]
        return result_holder["result"]

    def _fetch_agent_json(
        self, host: str, path: str, params: Optional[dict] = None
    ) -> Optional[dict]:
        cache_key = self._agent_json_cache_key(host, path, params)
        if cache_key in self._agent_json_cache:
            return deepcopy(self._agent_json_cache[cache_key])

        url = f"http://{host}{path}"
        started_at = time.perf_counter()
        status = "success"
        error: str | None = None
        try:
            response = httpx.get(url, params=params, timeout=5.0, trust_env=False)
            response.raise_for_status()
            data = response.json()
            self._agent_json_cache[cache_key] = deepcopy(data)
            return data
        except Exception as exc:
            status = "failure"
            error = str(exc)
            raise
        finally:
            SelfApmService.record_external_query(
                source="agent",
                operation=path,
                target=host,
                status=status,
                duration_ms=(time.perf_counter() - started_at) * 1000,
                error=error,
            )

    @staticmethod
    def _agent_json_cache_key(
        host: str,
        path: str,
        params: Optional[dict],
    ) -> tuple[str, str, str]:
        normalized_params = json.dumps(params or {}, sort_keys=True, default=str)
        return (host, path, normalized_params)

    def _fetch_agent_status(self, ctx: tuple[str, str]) -> Optional[dict[str, Any]]:
        host, token = ctx
        data = self._fetch_agent_json(host, f"/agent/runs/{token}/status")
        return data if isinstance(data, dict) else None

    def _parse_ts(self, ts_raw) -> Optional[datetime]:
        if not ts_raw:
            return None
        if isinstance(ts_raw, datetime):
            return ts_raw
        if isinstance(ts_raw, str):
            try:
                if ts_raw.endswith("Z"):
                    ts_raw = ts_raw.replace("Z", "+00:00")
                return datetime.fromisoformat(ts_raw)
            except Exception:
                return None
        return None

    def _fetch_agent_metrics(
        self, ctx: tuple[str, str], metric: Optional[str], step_seconds: int
    ) -> Optional[MetricsResponse]:
        host, token = ctx
        data = self._fetch_agent_json(host, f"/agent/runs/{token}/metrics")
        if not data:
            return None
        metric_map = {
            MetricName.RPS.value: MetricName.RPS,
            "rt_p95_ms": MetricName.RT_P95_MS,
            "rt_avg_ms": MetricName.RT_AVG_MS,
            "rt_p99_ms": MetricName.RT_P99_MS,
            MetricName.ERROR_RATE.value: MetricName.ERROR_RATE,
        }

        series: list[MetricsSeries] = []
        for s in data.get("series", []):
            metric_name = metric_map.get(s.get("metric"))
            if not metric_name:
                continue
            if metric and metric_name.value != metric:
                continue
            points: list[MetricPoint] = []
            for p in s.get("points", []):
                ts = self._parse_ts(p.get("ts"))
                if not ts:
                    continue
                points.append(
                    MetricPoint(
                        ts=ts,
                        value=(
                            float(p.get("value"))
                            if p.get("value") is not None
                            else None
                        ),
                    )
                )
            if points:
                series.append(
                    MetricsSeries(
                        metric=metric_name, unit=s.get("unit") or "", points=points
                    )
                )

        if not series:
            return None
        return MetricsResponse(
            step_seconds=int(data.get("step_seconds") or step_seconds), series=series
        )

    @staticmethod
    def _aggregate_metrics_point_value(
        metric_name: MetricName, values: list[float]
    ) -> float:
        if metric_name == MetricName.RPS:
            return round(sum(values), 4)
        if metric_name in {
            MetricName.RT_AVG_MS,
            MetricName.RT_P95_MS,
            MetricName.RT_P99_MS,
        }:
            return round(max(values), 4)
        if metric_name == MetricName.ERROR_RATE:
            return round(max(values), 6)
        return round(values[-1], 6)

    @staticmethod
    def _reduce_metrics_values_within_response_bucket(
        metric_name: MetricName,
        values: list[float],
    ) -> float:
        if metric_name == MetricName.RPS:
            return round(sum(values) / len(values), 4)
        if metric_name in {
            MetricName.RT_AVG_MS,
            MetricName.RT_P95_MS,
            MetricName.RT_P99_MS,
        }:
            return round(max(values), 4)
        if metric_name == MetricName.ERROR_RATE:
            return round(max(values), 6)
        return round(values[-1], 6)

    def _merge_metrics_responses(
        self,
        responses: list[MetricsResponse],
        *,
        step_seconds: int,
    ) -> Optional[MetricsResponse]:
        if not responses:
            return None

        bucketed_values: dict[MetricName, dict[int, list[float]]] = {}
        units_by_metric: dict[MetricName, str] = {}
        resolved_step_seconds = max(1, int(step_seconds or 1))

        for response in responses:
            response_bucketed_values: dict[MetricName, dict[int, list[float]]] = {}
            for series in response.series:
                metric_name = series.metric
                units_by_metric.setdefault(metric_name, series.unit)
                response_metric_buckets = response_bucketed_values.setdefault(
                    metric_name, {}
                )
                for point in series.points:
                    ts = self._as_utc(point.ts)
                    if ts is None or point.value is None:
                        continue
                    bucket_epoch = (
                        int(ts.timestamp() // resolved_step_seconds)
                        * resolved_step_seconds
                    )
                    response_metric_buckets.setdefault(bucket_epoch, []).append(
                        float(point.value)
                    )

            for (
                metric_name,
                response_metric_buckets,
            ) in response_bucketed_values.items():
                metric_buckets = bucketed_values.setdefault(metric_name, {})
                for bucket_epoch, values in response_metric_buckets.items():
                    if not values:
                        continue
                    metric_buckets.setdefault(bucket_epoch, []).append(
                        self._reduce_metrics_values_within_response_bucket(
                            metric_name, values
                        )
                    )

        merged_series: list[MetricsSeries] = []
        for metric_name, buckets in bucketed_values.items():
            points: list[MetricPoint] = []
            for bucket_epoch, values in sorted(buckets.items()):
                if not values:
                    continue
                points.append(
                    MetricPoint(
                        ts=datetime.fromtimestamp(bucket_epoch, tz=timezone.utc),
                        value=self._aggregate_metrics_point_value(metric_name, values),
                    )
                )
            if points:
                merged_series.append(
                    MetricsSeries(
                        metric=metric_name,
                        unit=units_by_metric.get(metric_name, ""),
                        points=points,
                    )
                )

        if not merged_series:
            return None
        return MetricsResponse(step_seconds=resolved_step_seconds, series=merged_series)

    def _fetch_agent_metrics_from_all_contexts(
        self,
        agent_contexts: list[tuple[str, str]],
        *,
        metric: Optional[str],
        step_seconds: int,
    ) -> Optional[MetricsResponse]:
        responses: list[MetricsResponse] = []
        for ctx in agent_contexts:
            response = self._fetch_agent_metrics(
                ctx, metric=metric, step_seconds=step_seconds
            )
            if response and response.series:
                responses.append(response)
        return self._merge_metrics_responses(responses, step_seconds=step_seconds)

    def _fetch_pushgateway_metrics(
        self, run_token: str, step_seconds: int, metric: Optional[str]
    ) -> Optional[MetricsResponse]:
        pushgateway = settings.PUSHGATEWAY_URL
        if not pushgateway:
            return None
        url = pushgateway.rstrip("/") + "/metrics"
        try:
            resp = httpx.get(url, timeout=5.0, trust_env=False)
            resp.raise_for_status()
            text = resp.text
        except Exception as exc:  # pragma: no cover - 容错
            logger.debug("fetch pushgateway metrics failed: %s", exc)
            return None

        gauge_pattern = re.compile(
            r'^ptp_run_(rps|rt_p95_ms|status)\{[^}]*run_token="%s"[^}]*\}\s+([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)$'
            % re.escape(run_token)
        )
        values: dict[str, float] = {}
        for line in text.splitlines():
            m = gauge_pattern.match(line.strip())
            if not m:
                continue
            key, val = m.group(1), m.group(2)
            try:
                values[key] = float(val)
            except ValueError:
                continue
        if not values:
            return None

        ts = datetime.now(timezone.utc).replace(microsecond=0)
        series: list[MetricsSeries] = []
        mapping = {"rps": MetricName.RPS, "rt_p95_ms": MetricName.RT_P95_MS}
        for key, metric_name in mapping.items():
            if key not in values:
                continue
            if metric and metric_name.value != metric:
                continue
            unit = "rps" if key == "rps" else "ms"
            val = values[key]
            series.append(
                MetricsSeries(
                    metric=metric_name,
                    unit=unit,
                    points=[MetricPoint(ts=ts, value=float(val))],
                )
            )
        return (
            MetricsResponse(step_seconds=step_seconds, series=series)
            if series
            else None
        )

    def _fetch_prometheus_metrics(
        self,
        run_token: str,
        start: datetime,
        end: datetime,
        step_seconds: int,
        metric: Optional[str],
    ) -> Optional[MetricsResponse]:
        prom = settings.PROMETHEUS_URL
        if not prom:
            return None
        base = prom.rstrip("/")
        queries = {
            "rps": f'ptp_run_rps{{run_token="{run_token}"}}',
            "rt_p95_ms": f'ptp_run_rt_p95_ms{{run_token="{run_token}"}}',
        }
        series: list[MetricsSeries] = []
        metric_to_query_key = {
            MetricName.RPS.value: "rps",
            MetricName.RT_P95_MS.value: "rt_p95_ms",
        }
        if metric:
            query_key = metric_to_query_key.get(metric)
            if not query_key:
                return None
            selected_keys = {query_key}
        else:
            selected_keys = set(queries.keys())
        start_ts = int(start.timestamp())
        end_ts = int(end.timestamp())
        for key, q in queries.items():
            if key not in selected_keys:
                continue
            try:
                resp = httpx.get(
                    f"{base}/api/v1/query_range",
                    params={
                        "query": q,
                        "start": start_ts,
                        "end": end_ts,
                        "step": step_seconds,
                    },
                    timeout=5.0,
                    trust_env=False,
                )
                resp.raise_for_status()
                payload = resp.json()
                if payload.get("status") != "success":
                    continue
                results = payload.get("data", {}).get("result") or []
                if not results:
                    continue
                values = results[0].get("values") or []
            except Exception as exc:  # pragma: no cover - 容错
                logger.debug("Prometheus range query failed for %s: %s", key, exc)
                continue

            points: list[MetricPoint] = []
            for val in values:
                if not val or len(val) < 2:
                    continue
                ts = datetime.fromtimestamp(float(val[0]), tz=timezone.utc)
                try:
                    value = float(val[1])
                except Exception:
                    continue
                points.append(MetricPoint(ts=ts, value=value))

            if not points:
                continue
            metric_name = {
                "rps": MetricName.RPS,
                "rt_p95_ms": MetricName.RT_P95_MS,
                "status": MetricName.ERROR_RATE,
            }.get(key)
            if not metric_name:
                continue
            unit = "rps" if key == "rps" else ("ms" if key == "rt_p95_ms" else "ratio")
            series.append(MetricsSeries(metric=metric_name, unit=unit, points=points))
        return (
            MetricsResponse(step_seconds=step_seconds, series=series)
            if series
            else None
        )

    def _fetch_prometheus_matrix(
        self,
        *,
        query: str,
        start: datetime,
        end: datetime,
        step_seconds: int,
    ) -> list[dict[str, Any]]:
        prom = settings.PROMETHEUS_URL
        if not prom:
            return []
        base = prom.rstrip("/")
        started_at = time.perf_counter()
        status = "success"
        error: str | None = None
        try:
            resp = httpx.get(
                f"{base}/api/v1/query_range",
                params={
                    "query": query,
                    "start": int(start.timestamp()),
                    "end": int(end.timestamp()),
                    "step": max(1, step_seconds),
                },
                timeout=5.0,
                trust_env=False,
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("status") != "success":
                return []
            return payload.get("data", {}).get("result") or []
        except Exception as exc:  # pragma: no cover - 容错
            status = "failure"
            error = str(exc)
            logger.debug("Prometheus matrix query failed for %s: %s", query, exc)
            return []
        finally:
            SelfApmService.record_external_query(
                source="prometheus",
                operation="query_range",
                target=base,
                status=status,
                duration_ms=(time.perf_counter() - started_at) * 1000,
                error=error,
            )

    def _fetch_prometheus_vector(
        self,
        *,
        query: str,
    ) -> list[dict[str, Any]]:
        prom = settings.PROMETHEUS_URL
        if not prom:
            return []
        base = prom.rstrip("/")
        started_at = time.perf_counter()
        status = "success"
        error: str | None = None
        try:
            resp = httpx.get(
                f"{base}/api/v1/query",
                params={"query": query},
                timeout=5.0,
                trust_env=False,
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("status") != "success":
                return []
            return payload.get("data", {}).get("result") or []
        except Exception as exc:  # pragma: no cover - 容错
            status = "failure"
            error = str(exc)
            logger.debug("Prometheus instant query failed for %s: %s", query, exc)
            return []
        finally:
            SelfApmService.record_external_query(
                source="prometheus",
                operation="query",
                target=base,
                status=status,
                duration_ms=(time.perf_counter() - started_at) * 1000,
                error=error,
            )

    @staticmethod
    def _build_prometheus_or_fallback_query(
        primary_query: str, fallback_query: Optional[str]
    ) -> str:
        if not fallback_query:
            return primary_query
        return f"({primary_query}) or ({fallback_query})"

    @staticmethod
    def _latest_matrix_value(result: dict[str, Any]) -> Optional[float]:
        values = result.get("values") if isinstance(result, dict) else None
        if not isinstance(values, list) or not values:
            return None
        for point in reversed(values):
            if not point or len(point) < 2:
                continue
            try:
                return float(point[1])
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _best_non_zero_series_value(points: list[MetricPoint]) -> Optional[float]:
        values = [
            float(point.value)
            for point in points
            if isinstance(point.value, (int, float))
        ]
        if not values:
            return None
        positive = [value for value in values if value > 0]
        values = positive or values
        values = sorted(values)
        mid = len(values) // 2
        if len(values) % 2 == 1:
            return values[mid]
        return (values[mid - 1] + values[mid]) / 2.0

    @staticmethod
    def _recent_tail_series_value(
        points: list[MetricPoint],
        *,
        recent_seconds: int = 30,
        tail_points: int = 3,
        prefer_non_zero: bool = True,
    ) -> Optional[float]:
        samples = [
            point
            for point in points
            if isinstance(point.value, (int, float)) and isinstance(point.ts, datetime)
        ]
        if not samples:
            return None
        latest_ts = max(point.ts for point in samples)
        cutoff = latest_ts - timedelta(seconds=max(0, recent_seconds))
        values = [float(point.value) for point in samples if point.ts >= cutoff] or [
            float(point.value) for point in samples
        ]
        if prefer_non_zero:
            positive = [value for value in values if value > 0]
            values = positive or values
        if tail_points > 0 and len(values) > tail_points:
            values = values[-tail_points:]
        values = sorted(values)
        mid = len(values) // 2
        if len(values) % 2 == 1:
            return values[mid]
        return (values[mid - 1] + values[mid]) / 2.0

    def _select_k6_throughput_series_value(
        self,
        run: Run,
        points: list[MetricPoint],
    ) -> Optional[float]:
        if self._is_k6_engine(run) and run.run_status == RunStatus.RUNNING:
            return self._recent_tail_series_value(points)
        return self._best_non_zero_series_value(points)

    @staticmethod
    def _mean_series_value(points: list[MetricPoint]) -> Optional[float]:
        values = [
            float(point.value)
            for point in points
            if isinstance(point.value, (int, float))
        ]
        if not values:
            return None
        return sum(values) / len(values)

    @staticmethod
    def _build_rate_points_from_counter_values(
        values: list[list[Any]],
        *,
        fallback_start_ts: Optional[datetime] = None,
    ) -> list[MetricPoint]:
        points: list[MetricPoint] = []
        previous_ts: Optional[datetime] = None
        previous_value: Optional[float] = None
        for raw_point in values:
            if not raw_point or len(raw_point) < 2:
                continue
            try:
                ts = datetime.fromtimestamp(float(raw_point[0]), tz=timezone.utc)
                value = float(raw_point[1])
            except (TypeError, ValueError):
                continue
            rate_value: Optional[float] = None
            if previous_ts is not None and previous_value is not None:
                elapsed = max(1e-9, (ts - previous_ts).total_seconds())
                rate_value = max(0.0, (value - previous_value) / elapsed)
            elif fallback_start_ts is not None and ts > fallback_start_ts:
                elapsed = max(1e-9, (ts - fallback_start_ts).total_seconds())
                rate_value = max(0.0, value / elapsed)
            if rate_value is not None:
                points.append(MetricPoint(ts=ts, value=round(rate_value, 6)))
            previous_ts = ts
            previous_value = value
        return points

    def _build_prometheus_live_window(
        self,
        run: Run,
        step_seconds: int,
    ) -> tuple[datetime, datetime]:
        ended_at = self._as_utc(run.ended_at)
        if ended_at is not None:
            start_ms, end_ms = self._build_dashboard_window_timestamps(run)
            return (
                datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc),
                datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc),
            )

        now = datetime.now(timezone.utc).replace(microsecond=0)
        start = self._as_utc(run.started_at) or self._as_utc(run.created_at) or now
        end = ended_at or now
        if end <= start:
            end = start + timedelta(seconds=max(30, step_seconds))
        return start, end

    def _fetch_prometheus_live_grpc_summary_metrics(
        self,
        run: Run,
        step_seconds: int,
    ) -> Optional[RunSummaryMetricsResponse]:
        if run.run_id is None:
            return None
        start, end = self._build_prometheus_live_window(run, step_seconds)
        record_id = int(run.run_id)
        queries = {
            "total_requests": f'histogram_count(sum by(name) (k6_grpc_req_duration_seconds{{recordId="{record_id}"}}))',
            "avg_rt_ms": f'histogram_avg(sum by(name) (k6_grpc_req_duration_seconds{{recordId="{record_id}"}}))',
            "p95_rt_ms": f'histogram_quantile(0.95, sum by(name) (k6_grpc_req_duration_seconds{{recordId="{record_id}"}}))',
            "p99_rt_ms": f'histogram_quantile(0.99, sum by(name) (k6_grpc_req_duration_seconds{{recordId="{record_id}"}}))',
            "max_rt_ms": f'histogram_quantile(1, sum by(name) (k6_grpc_req_duration_seconds{{recordId="{record_id}"}}))',
        }
        buckets: dict[str, dict[str, Any]] = {}
        counter_results: dict[str, list[list[Any]]] = {}
        for field_name, query in queries.items():
            for result in self._fetch_prometheus_matrix(
                query=query,
                start=start,
                end=end,
                step_seconds=step_seconds,
            ):
                endpoint_name = str(result.get("metric", {}).get("name") or "").strip()
                if not endpoint_name:
                    continue
                latest_value = self._latest_matrix_value(result)
                if latest_value is None:
                    continue
                bucket = buckets.setdefault(
                    endpoint_name, {"endpoint_name": endpoint_name}
                )
                if field_name == "total_requests":
                    counter_results[endpoint_name] = result.get("values") or []
                if field_name.endswith("_rt_ms") or field_name == "avg_rt_ms":
                    bucket[field_name] = round(latest_value * 1000.0, 6)
                elif field_name == "total_requests":
                    bucket[field_name] = int(latest_value)
                else:
                    bucket[field_name] = round(latest_value, 4)
        throughput_trends = self._fetch_prometheus_live_grpc_endpoint_trends(
            run,
            metric_filter=EndpointTrendMetric.THROUGHPUT.value,
            endpoint_filter=None,
            step_seconds=step_seconds,
        )
        throughput_by_endpoint = {
            item.endpoint_name: round(float(item.points[-1].value or 0.0), 4)
            for item in (throughput_trends.items if throughput_trends else [])
            if item.points
        }
        for endpoint_name, values in counter_results.items():
            bucket = buckets.setdefault(endpoint_name, {"endpoint_name": endpoint_name})
            if endpoint_name in throughput_by_endpoint:
                bucket["throughput"] = throughput_by_endpoint[endpoint_name]
                continue
            rate_points = self._build_rate_points_from_counter_values(
                values, fallback_start_ts=start
            )
            if rate_points:
                bucket["throughput"] = round(float(rate_points[-1].value or 0.0), 4)
        items = [
            RunSummaryMetricRow(**payload) for _, payload in sorted(buckets.items())
        ]
        return RunSummaryMetricsResponse(items=items) if items else None

    def _fetch_prometheus_live_http_summary_metrics(
        self,
        run: Run,
        step_seconds: int,
    ) -> Optional[RunSummaryMetricsResponse]:
        if run.run_id is None:
            return None
        start, end = self._build_prometheus_live_window(run, step_seconds)
        record_id = int(run.run_id)
        queries = {
            "total_requests": f'sum by(name) (k6_http_reqs_total{{recordId="{record_id}"}})',
            "avg_rt_ms": f'histogram_avg(sum by(name) (k6_http_req_duration_seconds{{recordId="{record_id}"}}))',
            "p95_rt_ms": f'histogram_quantile(0.95, sum by(name) (k6_http_req_duration_seconds{{recordId="{record_id}"}}))',
            "p99_rt_ms": f'histogram_quantile(0.99, sum by(name) (k6_http_req_duration_seconds{{recordId="{record_id}"}}))',
            "max_rt_ms": f'histogram_quantile(1, sum by(name) (k6_http_req_duration_seconds{{recordId="{record_id}"}}))',
        }
        buckets: dict[str, dict[str, Any]] = {}
        for field_name, query in queries.items():
            for result in self._fetch_prometheus_matrix(
                query=query,
                start=start,
                end=end,
                step_seconds=step_seconds,
            ):
                endpoint_name = self._normalize_endpoint_name(
                    result.get("metric", {}).get("name")
                )
                if not endpoint_name:
                    continue
                latest_value = self._latest_matrix_value(result)
                if latest_value is None:
                    continue
                bucket = buckets.setdefault(
                    endpoint_name, {"endpoint_name": endpoint_name}
                )
                if field_name.endswith("_rt_ms") or field_name == "avg_rt_ms":
                    bucket[field_name] = round(latest_value * 1000.0, 6)
                elif field_name == "total_requests":
                    bucket[field_name] = int(latest_value)
                else:
                    bucket[field_name] = round(latest_value, 4)

        throughput_trends = self._fetch_prometheus_live_http_endpoint_trends(
            run,
            metric_filter=EndpointTrendMetric.THROUGHPUT.value,
            endpoint_filter=None,
            step_seconds=step_seconds,
        )
        if throughput_trends and throughput_trends.items:
            for item in throughput_trends.items:
                if not item.points:
                    continue
                best_value = self._select_k6_throughput_series_value(run, item.points)
                if best_value is None:
                    continue
                bucket = buckets.setdefault(
                    item.endpoint_name, {"endpoint_name": item.endpoint_name}
                )
                bucket["throughput"] = round(best_value, 4)

        items = [
            RunSummaryMetricRow(**payload) for _, payload in sorted(buckets.items())
        ]
        return RunSummaryMetricsResponse(items=items) if items else None

    def _fetch_prometheus_live_grpc_checks(
        self,
        run: Run,
        step_seconds: int,
    ) -> Optional[RunChecksResponse]:
        if run.run_id is None:
            return None
        start, end = self._build_prometheus_live_window(run, step_seconds)
        record_id = int(run.run_id)
        query = f'avg by(check) (k6_checks_rate{{recordId="{record_id}"}})'
        results = self._fetch_prometheus_matrix(
            query=query,
            start=start,
            end=end,
            step_seconds=step_seconds,
        )
        if not results:
            results = self._fetch_prometheus_vector(query=query)
        items: list[RunCheckRow] = []
        for result in results:
            check_name = str(result.get("metric", {}).get("check") or "").strip()
            latest_value = self._latest_matrix_value(result)
            if latest_value is None:
                value = result.get("value") if isinstance(result, dict) else None
                if isinstance(value, list) and len(value) >= 2:
                    try:
                        latest_value = float(value[1])
                    except (TypeError, ValueError):
                        latest_value = None
            if not check_name or latest_value is None:
                continue
            items.append(
                RunCheckRow(
                    group_name="default",
                    check_name=check_name,
                    success_rate=max(0.0, min(1.0, latest_value)),
                )
            )
        return RunChecksResponse(items=items) if items else None

    def _fetch_prometheus_live_grpc_endpoint_trends(
        self,
        run: Run,
        metric_filter: Optional[str],
        endpoint_filter: Optional[str],
        step_seconds: int,
    ) -> Optional[EndpointTrendResponse]:
        if run.run_id is None:
            return None
        start, end = self._build_prometheus_live_window(run, step_seconds)
        record_id = int(run.run_id)
        range_window = f"{max(int(step_seconds or 10), 10)}s"
        metric_queries: dict[str, tuple[str, str, Optional[str]]] = {
            EndpointTrendMetric.THROUGHPUT.value: (
                f'histogram_count(sum by(name) (rate(k6_grpc_req_duration_seconds{{recordId="{record_id}"}}[{range_window}])))',
                "rps",
                None,
            ),
            EndpointTrendMetric.RT_AVG_MS.value: (
                f'histogram_avg(sum by(name) (rate(k6_grpc_req_duration_seconds{{recordId="{record_id}"}}[{range_window}])))',
                "ms",
                f'histogram_avg(sum by(name) (last_over_time(k6_grpc_req_duration_seconds{{recordId="{record_id}"}}[{range_window}])))',
            ),
            EndpointTrendMetric.RT_P95_MS.value: (
                f'histogram_quantile(0.95, sum by(name) (rate(k6_grpc_req_duration_seconds{{recordId="{record_id}"}}[{range_window}])))',
                "ms",
                f'histogram_quantile(0.95, sum by(name) (last_over_time(k6_grpc_req_duration_seconds{{recordId="{record_id}"}}[{range_window}])))',
            ),
            EndpointTrendMetric.RT_P99_MS.value: (
                f'histogram_quantile(0.99, sum by(name) (rate(k6_grpc_req_duration_seconds{{recordId="{record_id}"}}[{range_window}])))',
                "ms",
                f'histogram_quantile(0.99, sum by(name) (last_over_time(k6_grpc_req_duration_seconds{{recordId="{record_id}"}}[{range_window}])))',
            ),
        }
        requested_metrics = (
            [metric_filter] if metric_filter else list(metric_queries.keys())
        )
        items: list[EndpointTrendSeries] = []
        for metric_name in requested_metrics:
            spec = metric_queries.get(metric_name)
            if spec is None:
                continue
            query, unit, fallback_query = spec
            use_counter_fallback = False
            effective_query = self._build_prometheus_or_fallback_query(
                query, fallback_query
            )
            raw_results = self._fetch_prometheus_matrix(
                query=effective_query,
                start=start,
                end=end,
                step_seconds=step_seconds,
            )
            if metric_name == EndpointTrendMetric.THROUGHPUT.value and not raw_results:
                use_counter_fallback = True
                raw_results = self._fetch_prometheus_matrix(
                    query=f'histogram_count(sum by(name) (k6_grpc_req_duration_seconds{{recordId="{record_id}"}}))',
                    start=start,
                    end=end,
                    step_seconds=step_seconds,
                )
            for result in raw_results:
                endpoint_name = str(result.get("metric", {}).get("name") or "").strip()
                if not endpoint_name:
                    continue
                if endpoint_filter and endpoint_name != endpoint_filter:
                    continue
                points: list[MetricPoint] = []
                if (
                    metric_name == EndpointTrendMetric.THROUGHPUT.value
                    and use_counter_fallback
                ):
                    points = self._build_rate_points_from_counter_values(
                        result.get("values") or [], fallback_start_ts=start
                    )
                else:
                    for point in result.get("values") or []:
                        if not point or len(point) < 2:
                            continue
                        try:
                            ts = datetime.fromtimestamp(
                                float(point[0]), tz=timezone.utc
                            )
                            value = float(point[1])
                        except (TypeError, ValueError):
                            continue
                        if metric_name != EndpointTrendMetric.THROUGHPUT.value:
                            value *= 1000.0
                        points.append(MetricPoint(ts=ts, value=round(value, 6)))
                if (
                    metric_name != EndpointTrendMetric.THROUGHPUT.value
                    and points
                    and not any(
                        isinstance(point.value, (int, float))
                        and abs(float(point.value)) > 1e-9
                        for point in points
                    )
                ):
                    continue
                if points:
                    items.append(
                        EndpointTrendSeries(
                            endpoint_name=endpoint_name,
                            metric=EndpointTrendMetric(metric_name),
                            unit=unit,
                            points=points,
                        )
                    )
        return (
            EndpointTrendResponse(step_seconds=step_seconds, items=items)
            if items
            else None
        )

    def _fetch_prometheus_k6_endpoint_trends(
        self,
        run: Run,
        metric_filter: Optional[str],
        endpoint_filter: Optional[str],
        step_seconds: int,
    ) -> Optional[EndpointTrendResponse]:
        if run.run_id is None:
            return None

        protocol = str(self._resolve_run_protocol(run) or "").strip().lower()
        summary = (run.params or {}).get("k6_summary")
        metric_family = (
            str(summary.get("metric_family") or "").strip().lower()
            if isinstance(summary, dict)
            else ""
        )
        if (
            protocol == "mixed"
            or metric_family == "mixed"
            or self._is_k6_mixed_run(run)
        ):
            items: list[EndpointTrendSeries] = []
            http_trends = self._fetch_prometheus_live_http_endpoint_trends(
                run,
                metric_filter=metric_filter,
                endpoint_filter=endpoint_filter,
                step_seconds=step_seconds,
            )
            grpc_trends = self._fetch_prometheus_live_grpc_endpoint_trends(
                run,
                metric_filter=metric_filter,
                endpoint_filter=endpoint_filter,
                step_seconds=step_seconds,
            )
            if http_trends and http_trends.items:
                items.extend(http_trends.items)
            if grpc_trends and grpc_trends.items:
                items.extend(grpc_trends.items)
            if not items:
                return None
            deduped: dict[tuple[str, str], EndpointTrendSeries] = {}
            for item in items:
                deduped[(item.endpoint_name, item.metric.value)] = item
            return EndpointTrendResponse(
                step_seconds=step_seconds, items=list(deduped.values())
            )
        if (
            protocol == "grpc"
            or metric_family in {"grpc", "iteration"}
            or self._is_k6_grpc_or_iteration_run(run)
        ):
            return self._fetch_prometheus_live_grpc_endpoint_trends(
                run,
                metric_filter=metric_filter,
                endpoint_filter=endpoint_filter,
                step_seconds=step_seconds,
            )
        if protocol != "http":
            return None

        return self._fetch_prometheus_live_http_endpoint_trends(
            run,
            metric_filter=metric_filter,
            endpoint_filter=endpoint_filter,
            step_seconds=step_seconds,
        )

    def _merge_summary_metric_responses(
        self,
        *responses: Optional[RunSummaryMetricsResponse],
    ) -> Optional[RunSummaryMetricsResponse]:
        merged: dict[str, dict[str, Any]] = {}
        for response in responses:
            if not response or not response.items:
                continue
            for row in response.items:
                endpoint_name = str(row.endpoint_name or "").strip()
                if not endpoint_name:
                    continue
                payload = row.model_dump(exclude_none=True)
                merged[endpoint_name] = {**merged.get(endpoint_name, {}), **payload}
        if not merged:
            return None
        return RunSummaryMetricsResponse(
            items=[
                RunSummaryMetricRow(**payload) for _, payload in sorted(merged.items())
            ]
        )

    def _fetch_prometheus_live_http_endpoint_trends(
        self,
        run: Run,
        metric_filter: Optional[str],
        endpoint_filter: Optional[str],
        step_seconds: int,
    ) -> Optional[EndpointTrendResponse]:
        if run.run_id is None:
            return None

        start, end = self._build_prometheus_live_window(run, step_seconds)
        record_id = int(run.run_id)
        range_window = f"{max(int(step_seconds or 10), 10)}s"
        metric_queries: dict[str, tuple[str, str, bool, Optional[str]]] = {
            EndpointTrendMetric.THROUGHPUT.value: (
                f'sum by(name) (rate(k6_http_reqs_total{{recordId="{record_id}"}}[{range_window}]))',
                "rps",
                False,
                None,
            ),
            EndpointTrendMetric.RT_AVG_MS.value: (
                f'histogram_avg(sum by(name) (rate(k6_http_req_duration_seconds{{recordId="{record_id}"}}[{range_window}])))',
                "ms",
                False,
                f'histogram_avg(sum by(name) (last_over_time(k6_http_req_duration_seconds{{recordId="{record_id}"}}[{range_window}])))',
            ),
            EndpointTrendMetric.RT_P95_MS.value: (
                f'histogram_quantile(0.95, sum by(name) (rate(k6_http_req_duration_seconds{{recordId="{record_id}"}}[{range_window}])))',
                "ms",
                False,
                f'histogram_quantile(0.95, sum by(name) (last_over_time(k6_http_req_duration_seconds{{recordId="{record_id}"}}[{range_window}])))',
            ),
            EndpointTrendMetric.RT_P99_MS.value: (
                f'histogram_quantile(0.99, sum by(name) (rate(k6_http_req_duration_seconds{{recordId="{record_id}"}}[{range_window}])))',
                "ms",
                False,
                f'histogram_quantile(0.99, sum by(name) (last_over_time(k6_http_req_duration_seconds{{recordId="{record_id}"}}[{range_window}])))',
            ),
        }
        requested_metrics = (
            [metric_filter] if metric_filter else list(metric_queries.keys())
        )
        items: list[EndpointTrendSeries] = []
        for metric_name in requested_metrics:
            spec = metric_queries.get(metric_name)
            if spec is None:
                continue
            query, unit, treat_as_counter, fallback_query = spec
            effective_query = (
                self._build_prometheus_or_fallback_query(query, fallback_query)
                if not treat_as_counter
                else query
            )
            raw_results = self._fetch_prometheus_matrix(
                query=effective_query,
                start=start,
                end=end,
                step_seconds=step_seconds,
            )
            for result in raw_results:
                metric_payload = result.get("metric", {})
                endpoint = str(
                    metric_payload.get("name")
                    or metric_payload.get("endpoint_name")
                    or ""
                ).strip()
                if not endpoint:
                    continue
                if endpoint_filter and endpoint != endpoint_filter:
                    continue
                if treat_as_counter:
                    points = self._build_rate_points_from_counter_values(
                        result.get("values") or [],
                        fallback_start_ts=start,
                    )
                else:
                    points = []
                    for point in result.get("values") or []:
                        if not point or len(point) < 2:
                            continue
                        try:
                            ts = datetime.fromtimestamp(
                                float(point[0]), tz=timezone.utc
                            )
                            value = float(point[1])
                        except (TypeError, ValueError):
                            continue
                        if metric_name != EndpointTrendMetric.THROUGHPUT.value:
                            value *= 1000.0
                        points.append(MetricPoint(ts=ts, value=round(value, 6)))
                if (
                    not treat_as_counter
                    and points
                    and not any(
                        isinstance(point.value, (int, float))
                        and abs(float(point.value)) > 1e-9
                        for point in points
                    )
                ):
                    continue
                if points:
                    items.append(
                        EndpointTrendSeries(
                            endpoint_name=endpoint,
                            metric=EndpointTrendMetric(metric_name),
                            unit=unit,
                            points=points,
                        )
                    )
        return (
            EndpointTrendResponse(step_seconds=step_seconds, items=items)
            if items
            else None
        )

    def _fetch_agent_logs(
        self, ctx: tuple[str, str], cursor: Optional[str], limit: int, order: str
    ) -> Optional[LogsResponse]:
        host, token = ctx
        params = {"limit": limit, "order": order}
        if cursor:
            params["cursor"] = cursor
        data = self._fetch_agent_json(host, f"/agent/runs/{token}/logs", params=params)
        if not data:
            return None
        items: list[LogItem] = []
        for log in data.get("items", []):
            ts = self._parse_ts(log.get("ts"))
            if not ts:
                continue
            source_value = log.get("source")
            message_value = str(log.get("message"))
            source_value, message_value = self._split_prefixed_log_source(
                source_value, message_value
            )
            agent_host, agent_ip, source_kind, channel = self._build_log_facets(
                host=host,
                source=source_value,
            )
            items.append(
                LogItem(
                    seq=int(log.get("seq")),
                    ts=ts,
                    level=str(log.get("level")),
                    message=message_value,
                    source=source_value,
                    raw_message=message_value,
                    agent_host=agent_host,
                    agent_ip=agent_ip,
                    source_kind=source_kind,
                    channel=channel,
                )
            )
        start_seq = self._decode_cursor(cursor) if cursor else 0
        if order == "desc":
            filtered = [i for i in items if i.seq < start_seq or start_seq == 0]
            filtered.sort(key=lambda x: x.seq, reverse=True)
        else:
            filtered = [i for i in items if i.seq > start_seq]
            filtered.sort(key=lambda x: x.seq)
        sliced = filtered[:limit]
        raw_next_cursor = data.get("next_cursor")
        next_cursor = (
            str(raw_next_cursor)
            if raw_next_cursor not in {None, ""}
            else (self._encode_cursor(sliced[-1].seq) if sliced else None)
        )
        return LogsResponse(items=sliced, next_cursor=next_cursor)

    def _fetch_s3_logs(
        self, uri: str, cursor: Optional[str], limit: int, order: str
    ) -> Optional[LogsResponse]:
        bucket, key = s3_utils.parse_s3_uri(uri)
        content = s3_utils.download_bytes(bucket, key).decode("utf-8", errors="ignore")
        items: list[LogItem] = []
        lines = content.splitlines()
        base_ts = datetime.now(timezone.utc).replace(microsecond=0)
        fallback_seq = 1
        for line in content.splitlines():
            parts = line.rstrip("\n").split("|", 4)
            if len(parts) not in {4, 5}:
                text = line.strip()
                if not text:
                    continue
                items.append(
                    LogItem(
                        seq=fallback_seq,
                        ts=base_ts,
                        level="INFO",
                        message=text,
                        source="agent",
                        raw_message=text,
                    )
                )
                fallback_seq += 1
                continue
            try:
                seq = int(parts[0])
                ts = datetime.fromisoformat(parts[1])
                if len(parts) == 4:
                    level, source, message = parts[2], "agent", parts[3]
                else:
                    level, source, message = parts[2], parts[3], parts[4]
                source, message = self._split_prefixed_log_source(source, message)
                agent_host, agent_ip, source_kind, channel = self._build_log_facets(
                    host=None,
                    source=source,
                )
                items.append(
                    LogItem(
                        seq=seq,
                        ts=ts,
                        level=level,
                        message=message,
                        source=source or "agent",
                        raw_message=message,
                        agent_host=agent_host,
                        agent_ip=agent_ip,
                        source_kind=source_kind,
                        channel=channel,
                    )
                )
            except Exception:
                text = line.strip()
                if not text:
                    continue
                agent_host, agent_ip, source_kind, channel = self._build_log_facets(
                    host=None,
                    source="agent",
                )
                items.append(
                    LogItem(
                        seq=fallback_seq,
                        ts=base_ts,
                        level="INFO",
                        message=self._split_prefixed_log_source("agent", text)[1],
                        source="agent",
                        raw_message=self._split_prefixed_log_source("agent", text)[1],
                        agent_host=agent_host,
                        agent_ip=agent_ip,
                        source_kind=source_kind,
                        channel=channel,
                    )
                )
                fallback_seq += 1
        if not items and lines:
            for idx, raw in enumerate(lines, start=1):
                text = raw.strip()
                if not text:
                    continue
                agent_host, agent_ip, source_kind, channel = self._build_log_facets(
                    host=None,
                    source="agent",
                )
                items.append(
                    LogItem(
                        seq=idx,
                        ts=base_ts,
                        level="INFO",
                        message=text,
                        source="agent",
                        raw_message=text,
                        agent_host=agent_host,
                        agent_ip=agent_ip,
                        source_kind=source_kind,
                        channel=channel,
                    )
                )
        start_seq = self._decode_cursor(cursor) if cursor else 0
        if order == "desc":
            filtered = [i for i in items if i.seq < start_seq or start_seq == 0]
            filtered.sort(key=lambda x: x.seq, reverse=True)
        else:
            filtered = [i for i in items if i.seq > start_seq]
            filtered.sort(key=lambda x: x.seq)
        sliced = filtered[:limit]
        next_cursor = self._encode_cursor(sliced[-1].seq) if sliced else None
        return LogsResponse(items=sliced, next_cursor=next_cursor)

    def _merge_logs(
        self,
        log_batches: list[tuple[str, LogsResponse]],
        cursor: Optional[str],
        limit: int,
        order: str,
    ) -> Optional[LogsResponse]:
        normalized_items = self._normalize_log_batches(log_batches)
        if not normalized_items:
            return None
        return self._paginate_log_items(
            normalized_items, cursor=cursor, limit=limit, order=order
        )

    def _normalize_log_batches(
        self, log_batches: list[tuple[str, LogsResponse]]
    ) -> list[LogItem]:
        merged_items: list[LogItem] = []
        for source_label, response in log_batches:
            for item in response.items:
                agent_host, agent_ip, source_kind, channel = self._build_log_facets(
                    host=item.agent_host or source_label,
                    source=item.source,
                )
                merged_items.append(
                    LogItem(
                        seq=item.seq,
                        ts=item.ts,
                        level=item.level,
                        message=item.message,
                        source=(
                            f"{item.source}@{source_label}"
                            if item.source
                            else source_label
                        ),
                        raw_message=item.raw_message or item.message,
                        agent_host=agent_host,
                        agent_ip=item.agent_ip or agent_ip,
                        source_kind=item.source_kind or source_kind,
                        channel=item.channel or channel,
                    )
                )

        if not merged_items:
            return []

        merged_items.sort(
            key=lambda item: (item.ts, item.source or "", item.seq, item.message)
        )
        return [
            LogItem(
                seq=index,
                ts=item.ts,
                level=item.level,
                message=item.message,
                source=item.source,
                raw_message=item.raw_message,
                agent_host=item.agent_host,
                agent_ip=item.agent_ip,
                source_kind=item.source_kind,
                channel=item.channel,
            )
            for index, item in enumerate(merged_items, start=1)
        ]

    @staticmethod
    def _split_prefixed_log_source(
        source: Optional[str], message: str
    ) -> tuple[Optional[str], str]:
        source_text = str(source or "").strip()
        prefixes = {"ptp-agent", "ptp-admin", "tool-stdout", "tool-stderr"}
        for prefix in prefixes:
            marker = f"{prefix}|"
            if message.startswith(marker):
                return prefix, message[len(marker) :].lstrip()
        return source_text or None, message

    @staticmethod
    def _build_log_facets(
        *,
        host: Optional[str],
        source: Optional[str],
    ) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        host_text = str(host or "").strip() or None
        source_text = str(source or "").strip() or None
        agent_ip = host_text.split(":", 1)[0].strip() if host_text else None

        source_kind = "unknown"
        channel = "event"
        if source_text:
            lowered = source_text.lower()
            if lowered.startswith("tool-stdout"):
                source_kind = "tool"
                channel = "stdout"
            elif lowered.startswith("tool-stderr"):
                source_kind = "tool"
                channel = "stderr"
            elif (
                lowered.startswith("ptp-agent")
                or lowered.startswith("ptp-admin")
                or lowered == "agent"
            ):
                source_kind = "platform"
                channel = "event"

        return host_text, agent_ip, source_kind, channel

    def _paginate_log_items(
        self,
        items: list[LogItem],
        *,
        cursor: Optional[str],
        limit: int,
        order: str,
    ) -> LogsResponse:
        start_seq = self._decode_cursor(cursor) if cursor else 0
        if order == "desc":
            filtered = [
                item for item in items if item.seq < start_seq or start_seq == 0
            ]
            filtered.sort(key=lambda item: item.seq, reverse=True)
        else:
            filtered = [item for item in items if item.seq > start_seq]
            filtered.sort(key=lambda item: item.seq)
        sliced = filtered[:limit]
        next_cursor = self._encode_cursor(sliced[-1].seq) if sliced else None
        return LogsResponse(items=sliced, next_cursor=next_cursor)

    def _collect_agent_log_items(
        self,
        ctx: tuple[str, str],
        *,
        chunk_size: int = 1000,
        order: str = "asc",
        max_items: Optional[int] = None,
        view: str = "all",
        level: Optional[str] = None,
        source: Optional[str] = None,
    ) -> list[LogItem]:
        collected: list[LogItem] = []
        cursor: Optional[str] = None
        scanned = 0
        while True:
            remaining = max_items - scanned if max_items is not None else chunk_size
            if max_items is not None and remaining <= 0:
                break
            response = self._fetch_agent_logs(
                ctx,
                cursor=cursor,
                limit=(
                    min(chunk_size, remaining) if max_items is not None else chunk_size
                ),
                order=order,
            )
            if not response or not response.items:
                break
            scanned += len(response.items)
            collected.extend(
                self._filter_log_items(
                    response.items,
                    view=view,
                    level=level,
                    source=source,
                )
            )
            next_cursor = response.next_cursor
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        if order == "desc":
            collected.sort(key=lambda item: item.seq)
        return collected

    def _collect_agent_log_batches(
        self,
        contexts: list[tuple[str, str]],
        *,
        view: str = "all",
        level: Optional[str] = None,
        source: Optional[str] = None,
    ) -> list[tuple[str, LogsResponse]]:
        batches: list[tuple[str, LogsResponse]] = []
        for index, (host, token) in enumerate(contexts, start=1):
            try:
                items = self._collect_agent_log_items(
                    (host, token),
                    chunk_size=self.EXCEPTION_LOG_SCAN_CHUNK_SIZE,
                    order="desc" if view == "exception" else "asc",
                    max_items=(
                        self.EXCEPTION_LOG_SCAN_LIMIT_PER_SOURCE
                        if view == "exception"
                        else None
                    ),
                    view=view,
                    level=level,
                    source=source,
                )
            except Exception as exc:
                logger.warning("collect agent logs failed host=%s: %s", host, exc)
                continue
            if items:
                batches.append((host, LogsResponse(items=items, next_cursor=None)))
        return batches

    def _collect_s3_log_items(
        self,
        uri: str,
        *,
        chunk_size: int = 1000,
        order: str = "asc",
        max_items: Optional[int] = None,
        view: str = "all",
        level: Optional[str] = None,
        source: Optional[str] = None,
    ) -> list[LogItem]:
        collected: list[LogItem] = []
        cursor: Optional[str] = None
        scanned = 0
        while True:
            remaining = max_items - scanned if max_items is not None else chunk_size
            if max_items is not None and remaining <= 0:
                break
            response = self._fetch_s3_logs(
                uri,
                cursor=cursor,
                limit=(
                    min(chunk_size, remaining) if max_items is not None else chunk_size
                ),
                order=order,
            )
            if not response or not response.items:
                break
            scanned += len(response.items)
            collected.extend(
                self._filter_log_items(
                    response.items,
                    view=view,
                    level=level,
                    source=source,
                )
            )
            next_cursor = response.next_cursor
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        if order == "desc":
            collected.sort(key=lambda item: item.seq)
        return collected

    def _collect_s3_log_batches_from_run(
        self,
        run: Run,
        *,
        view: str = "all",
        level: Optional[str] = None,
        source: Optional[str] = None,
    ) -> list[tuple[str, LogsResponse]]:
        batches: list[tuple[str, LogsResponse]] = []
        for entry in self._iter_agent_run_entries(run):
            host = (
                str(entry.get("agent_host") or entry.get("agent_ip") or "agent").strip()
                or "agent"
            )
            for uri_key in ("log_s3", "k8s_log_s3"):
                s3_uri = entry.get(uri_key)
                if not isinstance(s3_uri, str) or not s3_uri:
                    continue
                try:
                    items = self._collect_s3_log_items(
                        s3_uri,
                        chunk_size=self.EXCEPTION_LOG_SCAN_CHUNK_SIZE,
                        order="desc" if view == "exception" else "asc",
                        max_items=(
                            self.EXCEPTION_LOG_SCAN_LIMIT_PER_SOURCE
                            if view == "exception"
                            else None
                        ),
                        view=view,
                        level=level,
                        source=source,
                    )
                except Exception as exc:
                    logger.warning(
                        "collect s3 logs failed run %s uri=%s: %s",
                        run.run_id,
                        s3_uri,
                        exc,
                    )
                    continue
                if items:
                    batches.append((host, LogsResponse(items=items, next_cursor=None)))
        return batches

    def _fetch_agent_logs_from_all_contexts(
        self,
        run_id: int,
        contexts: list[tuple[str, str]],
        cursor: Optional[str],
        limit: int,
        order: str,
    ) -> Optional[LogsResponse]:
        batches: list[tuple[str, LogsResponse]] = []
        for index, (host, token) in enumerate(contexts, start=1):
            try:
                response = self._fetch_agent_logs(
                    (host, token),
                    cursor=cursor,
                    limit=limit,
                    order=order,
                )
            except Exception as exc:
                logger.warning(
                    "get_logs agent failed for run %s host=%s: %s", run_id, host, exc
                )
                fallback = self._build_agent_log_fallback_item(
                    host,
                    reason="日志接口暂不可用，已保留该 agent 的运行上下文。",
                    seq=index,
                )
                batches.append((host, LogsResponse(items=[fallback], next_cursor=None)))
                continue
            if response and response.items:
                batches.append((host, response))
            else:
                fallback = self._build_agent_log_fallback_item(
                    host,
                    reason="本轮没有上报工具原生日志，已保留该 agent 的运行上下文。",
                    seq=index,
                )
                batches.append((host, LogsResponse(items=[fallback], next_cursor=None)))
        return self._merge_logs(batches, cursor=cursor, limit=limit, order=order)

    def _build_agent_log_fallback_item(
        self,
        host: str,
        *,
        reason: str,
        seq: int = 1,
        ts: Optional[datetime] = None,
    ) -> LogItem:
        agent_host, agent_ip, source_kind, channel = self._build_log_facets(
            host=host,
            source="ptp-agent",
        )
        message = f"{host} {reason}"
        return LogItem(
            seq=seq,
            ts=ts or datetime.now(timezone.utc).replace(microsecond=0),
            level="INFO",
            message=message,
            source="ptp-agent",
            raw_message=message,
            agent_host=agent_host,
            agent_ip=agent_ip,
            source_kind=source_kind,
            channel=channel,
        )

    def _fetch_s3_logs_from_all_uris(
        self,
        run: Run,
        cursor: Optional[str],
        limit: int,
        order: str,
    ) -> Optional[LogsResponse]:
        batches: list[tuple[str, LogsResponse]] = []
        for entry in self._iter_agent_run_entries(run):
            host = (
                str(entry.get("agent_host") or entry.get("agent_ip") or "agent").strip()
                or "agent"
            )
            for uri_key in ("log_s3", "k8s_log_s3"):
                s3_uri = entry.get(uri_key)
                if not isinstance(s3_uri, str) or not s3_uri:
                    continue
                try:
                    items = self._collect_s3_log_items(s3_uri)
                except Exception as exc:
                    logger.warning(
                        "get_logs s3 failed for run %s uri=%s: %s",
                        run.run_id,
                        s3_uri,
                        exc,
                    )
                    continue
                if items:
                    batches.append((host, LogsResponse(items=items, next_cursor=None)))
        return self._merge_logs(batches, cursor=cursor, limit=limit, order=order)

    def _fetch_s3_metrics(
        self,
        uri: str,
        metric: Optional[str],
        step_seconds: int,
        started_from: Optional[datetime],
        started_to: Optional[datetime],
    ) -> Optional[MetricsResponse]:
        bucket, key = s3_utils.parse_s3_uri(uri)
        try:
            raw_bytes = s3_utils.download_bytes(bucket, key)
            if key.endswith(".gz"):
                import gzip

                content = gzip.decompress(raw_bytes).decode("utf-8", errors="ignore")
            else:
                content = raw_bytes.decode("utf-8", errors="ignore")
        except Exception as exc:
            logger.warning("Failed to fetch or decode s3 metrics from %s: %s", uri, exc)
            return None

        if not content.strip():
            return None

        metric_map = {
            "rps": (MetricName.RPS, "rps"),
            "rt_avg_ms": (MetricName.RT_AVG_MS, "ms"),
            "rt_p95_ms": (MetricName.RT_P95_MS, "ms"),
            "rt_p99_ms": (MetricName.RT_P99_MS, "ms"),
            "error_rate": (MetricName.ERROR_RATE, "ratio"),
        }
        selected_metric = metric if metric in {m.value for m in MetricName} else None
        points_by_metric: dict[MetricName, list[MetricPoint]] = {}

        def append_point(metric_key: str, ts: datetime, value) -> None:
            mapped = metric_map.get(metric_key)
            if not mapped:
                return
            metric_name, _ = mapped
            if selected_metric and metric_name.value != selected_metric:
                return
            if value is None:
                return
            try:
                cast_value = float(value)
            except (TypeError, ValueError):
                return
            points_by_metric.setdefault(metric_name, []).append(
                MetricPoint(ts=ts, value=cast_value)
            )

        for raw in content.splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            ts = self._parse_ts(row.get("ts"))
            if not ts:
                continue
            ts_utc = self._as_utc(ts) or ts
            if started_from and ts_utc < started_from:
                continue
            if started_to and ts_utc > started_to:
                continue
            append_point("rps", ts_utc, row.get("rps"))
            append_point("rt_avg_ms", ts_utc, row.get("rt_avg_ms"))
            append_point("rt_p95_ms", ts_utc, row.get("rt_p95_ms"))
            append_point("rt_p99_ms", ts_utc, row.get("rt_p99_ms"))
            append_point("error_rate", ts_utc, row.get("error_rate"))

        series: list[MetricsSeries] = []
        for metric_name, points in points_by_metric.items():
            points.sort(key=lambda p: p.ts)
            unit = metric_map[metric_name.value][1]
            series.append(MetricsSeries(metric=metric_name, unit=unit, points=points))
        return (
            MetricsResponse(step_seconds=step_seconds, series=series)
            if series
            else None
        )

    # === Summary Metrics (接口级核心指标表) ===

    def get_summary_metrics(self, run_id: int) -> RunSummaryMetricsResponse:
        """获取接口级核心指标表。

        优先从 run.params.summary_metrics 获取种子数据（用于演示/测试），
        然后尝试从 agent 获取真实数据，最后返回空数组（不返回 500）。
        """
        run = self.repo.find_by_id(run_id)
        if not run:
            return RunSummaryMetricsResponse(items=[])
        self._sync_terminal_run_from_agent_status(run)
        self._attach_run_display_fields([run])

        params = run.params or {}
        has_real_metric_context = self._has_real_metric_context(run)
        real_metrics_summary: Optional[dict[str, float]] = None

        def align_summary_metrics_contract(
            response: RunSummaryMetricsResponse,
        ) -> RunSummaryMetricsResponse:
            nonlocal real_metrics_summary
            if not response.items or not has_real_metric_context:
                return response
            if real_metrics_summary is None:
                real_metrics_summary = self._build_summary_metrics_from_real_metrics(
                    run
                )
            return self._apply_real_metric_summary_contract(
                response, real_metrics_summary, run=run
            )

        # 1. 优先从 params.summary_metrics 获取种子数据
        seed_summary = self._extract_summary_metric_rows(params)
        persisted_real_summary: Optional[RunSummaryMetricsResponse] = None
        if seed_summary:
            items: list[RunSummaryMetricRow] = []
            for row in seed_summary:
                items.append(
                    RunSummaryMetricRow(
                        endpoint_name=row.get("endpoint_name", ""),
                        avg_rt_ms=row.get("avg_rt_ms"),
                        p95_rt_ms=row.get("p95_rt_ms"),
                        p99_rt_ms=row.get("p99_rt_ms"),
                        max_rt_ms=row.get("max_rt_ms"),
                        min_rt_ms=row.get("min_rt_ms"),
                        total_requests=row.get("total_requests"),
                        throughput=row.get("throughput"),
                    )
                )
            if items:
                seed_response = align_summary_metrics_contract(
                    RunSummaryMetricsResponse(items=items)
                )
                if not has_real_metric_context:
                    return seed_response
                persisted_real_summary = seed_response
                if self._is_terminal_run_status(run.run_status):
                    return persisted_real_summary

        # 2. 尝试从 agent 获取
        agent_contexts = self._get_agent_contexts(run)
        agent_summary: Optional[RunSummaryMetricsResponse] = None
        if agent_contexts:
            agent_summary = self._fetch_agent_summary_metrics_from_all_contexts(
                run_id, agent_contexts
            )

        if self._is_k6_mixed_run(run):
            mixed_summary = self._merge_summary_metric_responses(
                agent_summary,
                self._fetch_prometheus_live_http_summary_metrics(run, step_seconds=10),
                self._fetch_prometheus_live_grpc_summary_metrics(run, step_seconds=10),
            )
            if mixed_summary and mixed_summary.items:
                return align_summary_metrics_contract(mixed_summary)

        if agent_summary and agent_summary.items:
            return align_summary_metrics_contract(agent_summary)

        if persisted_real_summary and persisted_real_summary.items:
            return persisted_real_summary

        if self._is_k6_grpc_or_iteration_run(run):
            prom_summary = self._fetch_prometheus_live_grpc_summary_metrics(
                run, step_seconds=10
            )
            if prom_summary and prom_summary.items:
                return align_summary_metrics_contract(prom_summary)

        if run.run_status == RunStatus.RUNNING and self._is_k6_engine(run):
            prom_http_summary = self._fetch_prometheus_live_http_summary_metrics(
                run, step_seconds=10
            )
            if prom_http_summary and prom_http_summary.items:
                return align_summary_metrics_contract(prom_http_summary)

        # 3. 尝试从 S3 归档获取
        metrics_s3_uri = params.get("metrics_s3")
        if metrics_s3_uri:
            try:
                summary = self._fetch_s3_summary_metrics(metrics_s3_uri)
                if summary and summary.items:
                    return align_summary_metrics_contract(summary)
            except Exception as exc:
                logger.warning(
                    "get_summary_metrics s3 failed for run %s: %s", run_id, exc
                )

        if has_real_metric_context and real_metrics_summary is None:
            real_metrics_summary = self._build_summary_metrics_from_real_metrics(run)
        fallback = self._build_fallback_summary_metrics(
            run, metrics_summary=real_metrics_summary
        )
        if fallback and fallback.items:
            return align_summary_metrics_contract(fallback)

        # 4. 返回空数组，不返回 500
        return RunSummaryMetricsResponse(items=[])

    def _build_fallback_summary_metrics(
        self,
        run: Run,
        metrics_summary: Optional[dict[str, float]] = None,
    ) -> Optional[RunSummaryMetricsResponse]:
        fallback_values = self._build_overall_summary_metric_fallback_values(run)
        metrics_summary = (
            metrics_summary
            if metrics_summary is not None
            else self._build_summary_metrics_from_real_metrics(run)
        )
        total_requests = fallback_values.get("total_requests")
        throughput = metrics_summary.get("throughput")
        if throughput is None:
            throughput = fallback_values.get("throughput")
        avg_rt_ms = metrics_summary.get("avg_rt_ms")
        if avg_rt_ms is None:
            avg_rt_ms = fallback_values.get("avg_rt_ms")
        p95_rt_ms = metrics_summary.get("p95_rt_ms")
        if p95_rt_ms is None:
            p95_rt_ms = fallback_values.get("p95_rt_ms")
        p99_rt_ms = metrics_summary.get("p99_rt_ms")
        if p99_rt_ms is None:
            p99_rt_ms = fallback_values.get("p99_rt_ms")
        max_rt_ms = metrics_summary.get("max_rt_ms")
        if max_rt_ms is None:
            max_rt_ms = fallback_values.get("max_rt_ms")
        min_rt_ms = metrics_summary.get("min_rt_ms")
        if min_rt_ms is None:
            min_rt_ms = fallback_values.get("min_rt_ms")

        if all(
            value is None
            for value in (total_requests, throughput, avg_rt_ms, p95_rt_ms, p99_rt_ms)
        ):
            return None

        fallback_endpoint_name = (
            self._infer_single_grpc_iteration_endpoint_name(run) or "overall"
        )

        return RunSummaryMetricsResponse(
            items=[
                RunSummaryMetricRow(
                    endpoint_name=fallback_endpoint_name,
                    avg_rt_ms=avg_rt_ms,
                    p95_rt_ms=p95_rt_ms,
                    p99_rt_ms=p99_rt_ms,
                    max_rt_ms=max_rt_ms,
                    min_rt_ms=min_rt_ms,
                    total_requests=total_requests,
                    throughput=throughput,
                )
            ]
        )

    def _build_summary_metrics_from_real_metrics(self, run: Run) -> dict[str, float]:
        contract_throughput: Optional[float] = None
        if self._is_k6_engine(run):
            params = run.params if isinstance(run.params, dict) else {}
            _, contract_throughput = self._resolve_k6_overview_contract_totals(run)
            terminal_k6_throughput = self._resolve_terminal_k6_summary_throughput(
                run,
                params,
                contract_throughput=contract_throughput,
            )
            if terminal_k6_throughput is not None:
                contract_throughput = terminal_k6_throughput

        def _series_stats(metric_name: MetricName) -> Optional[dict[str, float]]:
            response = self._get_real_metrics_response(
                run,
                metric=metric_name.value,
                step_seconds=10,
            )
            if not response or not response.series:
                return None
            if (
                metric_name == MetricName.RPS
                and self._is_k6_engine(run)
                and run.run_status == RunStatus.RUNNING
            ):
                tail_values = [
                    value
                    for value in (
                        self._recent_tail_series_value(series.points)
                        for series in response.series
                    )
                    if isinstance(value, (int, float))
                ]
                if tail_values:
                    return {
                        "mean": float(sum(tail_values)),
                        "min": min(float(value) for value in tail_values),
                        "max": max(float(value) for value in tail_values),
                    }
            values = [
                float(point.value)
                for series in response.series
                for point in series.points
                if isinstance(point.value, (int, float))
            ]
            if not values:
                return None
            return {
                "mean": sum(values) / len(values),
                "min": min(values),
                "max": max(values),
            }

        throughput_stats = _series_stats(MetricName.RPS)
        avg_stats = _series_stats(MetricName.RT_AVG_MS)
        p95_stats = _series_stats(MetricName.RT_P95_MS)
        p99_stats = _series_stats(MetricName.RT_P99_MS)

        summary: dict[str, float] = {}
        if contract_throughput is not None:
            summary["throughput"] = contract_throughput
        elif throughput_stats:
            summary["throughput"] = throughput_stats["mean"]
        if avg_stats:
            summary["avg_rt_ms"] = avg_stats["mean"]
            summary["min_rt_ms"] = avg_stats["min"]
        if p95_stats:
            summary["p95_rt_ms"] = p95_stats["mean"]
        if p99_stats:
            summary["p99_rt_ms"] = p99_stats["mean"]
            summary["max_rt_ms"] = p99_stats["max"]
        return summary

    def _apply_real_metric_summary_contract(
        self,
        response: RunSummaryMetricsResponse,
        metrics_summary: dict[str, float],
        run: Optional[Run] = None,
    ) -> RunSummaryMetricsResponse:
        if not response.items:
            return response

        per_endpoint_total_requests: dict[str, int] = {}
        per_endpoint_throughput: dict[str, float] = {}
        per_endpoint_latency_means: dict[str, dict[str, float]] = {}
        if run is not None and self._is_k6_engine(run):
            (
                per_endpoint_total_requests,
                per_endpoint_throughput,
                per_endpoint_latency_means,
            ) = self._fetch_prometheus_live_k6_endpoint_contract_fields(
                run, step_seconds=10
            )
        endpoint_contract_names = (
            set(per_endpoint_total_requests)
            | set(per_endpoint_throughput)
            | set(per_endpoint_latency_means)
        )
        single_endpoint_contract_name = (
            next(iter(endpoint_contract_names))
            if len(response.items) == 1 and len(endpoint_contract_names) == 1
            else None
        )

        aligned_items: list[RunSummaryMetricRow] = []
        for row in response.items:
            payload = row.model_dump()
            aligned_endpoint_name = row.endpoint_name
            if row.endpoint_name == "overall" and single_endpoint_contract_name:
                aligned_endpoint_name = single_endpoint_contract_name
                payload["endpoint_name"] = aligned_endpoint_name

            if aligned_endpoint_name != "overall":
                if aligned_endpoint_name in per_endpoint_total_requests:
                    payload["total_requests"] = per_endpoint_total_requests[
                        aligned_endpoint_name
                    ]
                if aligned_endpoint_name in per_endpoint_throughput:
                    payload["throughput"] = per_endpoint_throughput[
                        aligned_endpoint_name
                    ]
                latency_contract = (
                    per_endpoint_latency_means.get(aligned_endpoint_name) or {}
                )
                for field_name in ("avg_rt_ms", "p95_rt_ms", "p99_rt_ms"):
                    if field_name in latency_contract:
                        payload[field_name] = latency_contract[field_name]
                aligned_items.append(RunSummaryMetricRow(**payload))
                continue

            if "throughput" in metrics_summary:
                payload["throughput"] = metrics_summary["throughput"]
            if "avg_rt_ms" in metrics_summary:
                payload["avg_rt_ms"] = metrics_summary["avg_rt_ms"]
            if "min_rt_ms" in metrics_summary:
                payload["min_rt_ms"] = metrics_summary["min_rt_ms"]
            if "p95_rt_ms" in metrics_summary:
                payload["p95_rt_ms"] = metrics_summary["p95_rt_ms"]
            if "p99_rt_ms" in metrics_summary:
                payload["p99_rt_ms"] = metrics_summary["p99_rt_ms"]
            if "max_rt_ms" in metrics_summary:
                payload["max_rt_ms"] = metrics_summary["max_rt_ms"]
            aligned_items.append(RunSummaryMetricRow(**payload))
        return RunSummaryMetricsResponse(items=aligned_items)

    def _fetch_prometheus_live_k6_endpoint_contract_fields(
        self,
        run: Run,
        step_seconds: int,
    ) -> tuple[dict[str, int], dict[str, float], dict[str, dict[str, float]]]:
        if run.run_id is None or not self._is_k6_engine(run):
            return {}, {}, {}

        start, end = self._build_prometheus_live_window(run, step_seconds)
        record_id = int(run.run_id)
        total_requests_by_endpoint: dict[str, int] = {}
        throughput_by_endpoint: dict[str, float] = {}
        throughput_points_count_by_endpoint: dict[str, int] = {}
        latency_mean_by_endpoint: dict[str, dict[str, float]] = {}
        counter_queries = [
            f'sum by(name) (k6_http_reqs_total{{recordId="{record_id}"}})',
            f'histogram_count(sum by(name) (k6_grpc_req_duration_seconds{{recordId="{record_id}"}}))',
        ]

        for query in counter_queries:
            for result in self._fetch_prometheus_matrix(
                query=query,
                start=start,
                end=end,
                step_seconds=step_seconds,
            ):
                endpoint_name = str(result.get("metric", {}).get("name") or "").strip()
                if not endpoint_name:
                    continue
                latest_value = self._latest_matrix_value(result)
                if latest_value is not None:
                    total_requests_by_endpoint[endpoint_name] = int(round(latest_value))

        throughput_trends = self._fetch_prometheus_k6_endpoint_trends(
            run,
            metric_filter=EndpointTrendMetric.THROUGHPUT.value,
            endpoint_filter=None,
            step_seconds=step_seconds,
        )
        if throughput_trends and throughput_trends.items:
            for item in throughput_trends.items:
                representative_value = self._select_k6_throughput_series_value(
                    run, item.points
                )
                if representative_value is None:
                    continue
                throughput_points_count_by_endpoint[item.endpoint_name] = len(
                    [
                        point
                        for point in item.points
                        if isinstance(getattr(point, "value", None), (int, float))
                    ]
                )
                throughput_by_endpoint[item.endpoint_name] = round(
                    representative_value, 4
                )

        params = run.params if isinstance(run.params, dict) else {}
        k6_summary = (
            params.get("k6_summary")
            if isinstance(params.get("k6_summary"), dict)
            else {}
        )
        if self._is_terminal_run_status(run.run_status):
            summary_throughput = self._parse_seed_float(k6_summary.get("throughput"))
            if summary_throughput is not None and len(throughput_by_endpoint) == 1:
                only_endpoint = next(iter(throughput_by_endpoint.keys()))
                representative = throughput_by_endpoint.get(only_endpoint)
                point_count = throughput_points_count_by_endpoint.get(only_endpoint, 0)
                if representative is None or representative <= 0:
                    throughput_by_endpoint[only_endpoint] = round(
                        float(summary_throughput), 4
                    )
                else:
                    delta_ratio = abs(
                        float(summary_throughput) - float(representative)
                    ) / max(
                        abs(float(representative)),
                        1.0,
                    )
                    if point_count <= 6 or delta_ratio <= 0.1:
                        throughput_by_endpoint[only_endpoint] = round(
                            float(summary_throughput), 4
                        )

        metric_field_map = {
            EndpointTrendMetric.RT_AVG_MS.value: "avg_rt_ms",
            EndpointTrendMetric.RT_P95_MS.value: "p95_rt_ms",
            EndpointTrendMetric.RT_P99_MS.value: "p99_rt_ms",
        }
        for metric_name, field_name in metric_field_map.items():
            latency_trends = self._fetch_prometheus_k6_endpoint_trends(
                run,
                metric_filter=metric_name,
                endpoint_filter=None,
                step_seconds=step_seconds,
            )
            if not latency_trends or not latency_trends.items:
                continue
            for item in latency_trends.items:
                mean_value = self._mean_series_value(item.points)
                if mean_value is None:
                    continue
                bucket = latency_mean_by_endpoint.setdefault(item.endpoint_name, {})
                bucket[field_name] = round(mean_value, 6)

        return (
            total_requests_by_endpoint,
            throughput_by_endpoint,
            latency_mean_by_endpoint,
        )

    def _build_overall_summary_metric_fallback_values(
        self, run: Run
    ) -> dict[str, float | int]:
        params = run.params or {}
        summary = getattr(
            run, "overview_summary", None
        ) or self._build_run_overview_summary(run, params)
        summary_rows = self._extract_summary_metric_rows(params)
        k6_summary = (
            params.get("k6_summary")
            if isinstance(params.get("k6_summary"), dict)
            else {}
        )
        overall_row = next(
            (
                row
                for row in summary_rows
                if isinstance(row, dict)
                and (row.get("endpoint_name") or "overall") == "overall"
            ),
            None,
        )

        def pick_int(*values) -> Optional[int]:
            return self._pick_first_seed_int(*values)

        def pick_float(*values) -> Optional[float]:
            for value in values:
                parsed = self._parse_seed_float(value)
                if parsed is not None:
                    return parsed
            return None

        avg_rt_ms = pick_float(
            overall_row.get("avg_rt_ms") if overall_row else None,
            summary.avg_rt_ms if summary else None,
            run.avg_rt_ms,
            k6_summary.get("rt_avg_ms"),
        )
        p99_rt_ms = pick_float(
            overall_row.get("p99_rt_ms") if overall_row else None,
            run.p99_rt_ms,
            k6_summary.get("rt_p99_ms"),
        )

        fallback_values: dict[str, float | int] = {}
        total_requests = pick_int(
            overall_row.get("total_requests") if overall_row else None,
            summary.total_requests if summary else None,
            run.total_requests,
            self._build_k6_iteration_fallback_total_requests(run, k6_summary),
        )
        if total_requests is not None:
            fallback_values["total_requests"] = total_requests
        throughput = pick_float(
            overall_row.get("throughput") if overall_row else None,
            summary.throughput if summary else None,
            run.rps,
            k6_summary.get("throughput"),
        )
        if throughput is not None:
            fallback_values["throughput"] = throughput
        if avg_rt_ms is not None:
            fallback_values["avg_rt_ms"] = avg_rt_ms
        p95_rt_ms = pick_float(
            overall_row.get("p95_rt_ms") if overall_row else None,
            summary.p95_rt_ms if summary else None,
            run.p95_rt_ms,
            k6_summary.get("rt_p95_ms"),
        )
        if p95_rt_ms is not None:
            fallback_values["p95_rt_ms"] = p95_rt_ms
        if p99_rt_ms is not None:
            fallback_values["p99_rt_ms"] = p99_rt_ms
        min_rt_ms = pick_float(
            overall_row.get("min_rt_ms") if overall_row else None,
            avg_rt_ms,
        )
        if min_rt_ms is not None:
            fallback_values["min_rt_ms"] = min_rt_ms
        max_rt_ms = pick_float(
            overall_row.get("max_rt_ms") if overall_row else None,
            k6_summary.get("rt_max_ms"),
            p99_rt_ms,
        )
        if max_rt_ms is not None:
            fallback_values["max_rt_ms"] = max_rt_ms
        error_rate = pick_float(
            overall_row.get("error_rate") if overall_row else None,
            summary.error_rate if summary else None,
            run.error_rate,
            k6_summary.get("error_rate"),
        )
        if error_rate is not None:
            fallback_values["error_rate"] = error_rate
        return fallback_values

    def _build_overall_endpoint_trend_fallback_items(
        self,
        run: Run,
        metric_filter: Optional[str],
        step_seconds: int,
        existing_metrics: Optional[set[EndpointTrendMetric]] = None,
        endpoint_name: str = "overall",
    ) -> list[EndpointTrendSeries]:
        existing_metrics = existing_metrics or set()
        fallback_values = self._build_overall_summary_metric_fallback_values(run)
        base_ts = (
            self._as_utc(run.ended_at)
            or self._as_utc(run.started_at)
            or self._as_utc(run.created_at)
            or datetime.now(timezone.utc).replace(microsecond=0)
        )
        metric_specs = [
            (EndpointTrendMetric.THROUGHPUT, "throughput", "rps"),
            (EndpointTrendMetric.RT_AVG_MS, "avg_rt_ms", "ms"),
            (EndpointTrendMetric.RT_P95_MS, "p95_rt_ms", "ms"),
            (EndpointTrendMetric.RT_P99_MS, "p99_rt_ms", "ms"),
            (EndpointTrendMetric.ERROR_RATE, "error_rate", "ratio"),
        ]

        items: list[EndpointTrendSeries] = []
        for endpoint_metric, payload_key, unit in metric_specs:
            if metric_filter and endpoint_metric.value != metric_filter:
                continue
            if endpoint_metric in existing_metrics:
                continue
            value = fallback_values.get(payload_key)
            if value is None:
                continue
            items.append(
                EndpointTrendSeries(
                    endpoint_name=endpoint_name,
                    metric=endpoint_metric,
                    unit=unit,
                    points=[MetricPoint(ts=base_ts, value=float(value))],
                )
            )
        return items

    def _build_summary_row_endpoint_trend_fallback_items(
        self,
        run: Run,
        metric_filter: Optional[str],
        endpoint_filter: Optional[str],
        step_seconds: int,
    ) -> list[EndpointTrendSeries]:
        params = run.params or {}
        summary_rows = self._extract_summary_metric_rows(params)
        if not summary_rows:
            return []

        start_ts = (
            self._as_utc(run.started_at)
            or self._as_utc(run.created_at)
            or datetime.now(timezone.utc).replace(microsecond=0)
        )
        end_ts = self._as_utc(run.ended_at) or (
            start_ts + timedelta(seconds=max(step_seconds, 1))
        )
        if end_ts <= start_ts:
            end_ts = start_ts + timedelta(seconds=max(step_seconds, 1))

        metric_specs = [
            (EndpointTrendMetric.THROUGHPUT, "throughput", "rps"),
            (EndpointTrendMetric.RT_AVG_MS, "avg_rt_ms", "ms"),
            (EndpointTrendMetric.RT_P95_MS, "p95_rt_ms", "ms"),
            (EndpointTrendMetric.RT_P99_MS, "p99_rt_ms", "ms"),
        ]

        items: list[EndpointTrendSeries] = []
        for row in summary_rows:
            if not isinstance(row, dict):
                continue
            endpoint_name = str(
                row.get("endpoint_name") or row.get("name") or ""
            ).strip()
            if not endpoint_name or endpoint_name == "overall":
                continue
            if endpoint_filter and endpoint_name != endpoint_filter:
                continue

            for endpoint_metric, payload_key, unit in metric_specs:
                if metric_filter and endpoint_metric.value != metric_filter:
                    continue
                value = self._parse_seed_float(row.get(payload_key))
                if value is None:
                    continue
                items.append(
                    EndpointTrendSeries(
                        endpoint_name=endpoint_name,
                        metric=endpoint_metric,
                        unit=unit,
                        points=[
                            MetricPoint(ts=start_ts, value=float(value)),
                            MetricPoint(ts=end_ts, value=float(value)),
                        ],
                    )
                )
        return items

    def _collect_real_metrics_by_metric(
        self,
        run: Run,
        metric_names: list[str],
        step_seconds: int,
    ) -> Optional[MetricsResponse]:
        merged_series: list[MetricsSeries] = []
        response_step_seconds = step_seconds
        seen_metrics: set[MetricName] = set()

        for metric_name in metric_names:
            response = self._get_real_metrics_response(
                run,
                metric=metric_name,
                step_seconds=step_seconds,
            )
            if not response or not response.series:
                continue
            response_step_seconds = response.step_seconds or response_step_seconds
            for series in response.series:
                if series.metric in seen_metrics:
                    continue
                seen_metrics.add(series.metric)
                merged_series.append(series)

        if not merged_series:
            return None
        return MetricsResponse(step_seconds=response_step_seconds, series=merged_series)

    def _fetch_agent_summary_metrics(
        self, ctx: tuple[str, str]
    ) -> Optional[RunSummaryMetricsResponse]:
        """从 agent 获取接口级核心指标。"""
        host, token = ctx
        data = self._fetch_agent_json(host, f"/agent/runs/{token}/summary-metrics")
        if not data or not data.get("items"):
            return None

        items: list[RunSummaryMetricRow] = []
        for row in data.get("items", []):
            items.append(
                RunSummaryMetricRow(
                    endpoint_name=row.get("endpoint_name", ""),
                    avg_rt_ms=row.get("avg_rt_ms"),
                    p95_rt_ms=row.get("p95_rt_ms"),
                    p99_rt_ms=row.get("p99_rt_ms"),
                    max_rt_ms=row.get("max_rt_ms"),
                    min_rt_ms=row.get("min_rt_ms"),
                    total_requests=row.get("total_requests"),
                    throughput=row.get("throughput"),
                )
            )
        return RunSummaryMetricsResponse(items=items)

    def _fetch_s3_summary_metrics(
        self, uri: str
    ) -> Optional[RunSummaryMetricsResponse]:
        """从 S3 归档获取接口级核心指标。"""
        bucket, key = s3_utils.parse_s3_uri(uri)
        started_at = time.perf_counter()
        status = "success"
        error: str | None = None
        try:
            raw_bytes = s3_utils.download_bytes(bucket, key)
            if key.endswith(".gz"):
                import gzip

                content = gzip.decompress(raw_bytes).decode("utf-8", errors="ignore")
            else:
                content = raw_bytes.decode("utf-8", errors="ignore")
        except Exception as exc:
            status = "failure"
            error = str(exc)
            logger.warning("Failed to fetch s3 summary metrics from %s: %s", uri, exc)
            return None
        finally:
            SelfApmService.record_external_query(
                source="s3",
                operation="summary_metrics",
                target=bucket,
                status=status,
                duration_ms=(time.perf_counter() - started_at) * 1000,
                error=error,
            )

        if not content.strip():
            return None

        items: list[RunSummaryMetricRow] = []
        for raw in content.splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            # 尝试解析 summary 格式
            if "endpoint_name" in row or "name" in row:
                items.append(
                    RunSummaryMetricRow(
                        endpoint_name=row.get("endpoint_name") or row.get("name", ""),
                        avg_rt_ms=row.get("avg_rt_ms"),
                        p95_rt_ms=row.get("p95_rt_ms"),
                        p99_rt_ms=row.get("p99_rt_ms"),
                        max_rt_ms=row.get("max_rt_ms"),
                        min_rt_ms=row.get("min_rt_ms"),
                        total_requests=row.get("total_requests"),
                        throughput=row.get("throughput"),
                    )
                )

        return RunSummaryMetricsResponse(items=items) if items else None

    def _merge_summary_metric_rows(
        self,
        rows: list[RunSummaryMetricRow],
    ) -> RunSummaryMetricsResponse:
        merged: dict[str, dict[str, Any]] = {}

        for row in rows:
            endpoint_name = str(row.endpoint_name or "").strip()
            if not endpoint_name:
                continue
            bucket = merged.setdefault(
                endpoint_name,
                {
                    "endpoint_name": endpoint_name,
                    "_avg_weight_total": 0.0,
                    "_avg_weight_count": 0.0,
                },
            )
            total_requests = row.total_requests
            throughput = row.throughput
            if total_requests is not None:
                bucket["total_requests"] = int(bucket.get("total_requests") or 0) + int(
                    total_requests
                )
            if throughput is not None:
                bucket["throughput"] = float(bucket.get("throughput") or 0.0) + float(
                    throughput
                )
            if row.avg_rt_ms is not None:
                weight = float(total_requests or 1)
                bucket["_avg_weight_total"] += float(row.avg_rt_ms) * weight
                bucket["_avg_weight_count"] += weight
                bucket["avg_rt_ms"] = (
                    bucket["_avg_weight_total"] / bucket["_avg_weight_count"]
                )
            for field_name in ("p95_rt_ms", "p99_rt_ms", "max_rt_ms"):
                value = getattr(row, field_name)
                if value is None:
                    continue
                current = bucket.get(field_name)
                bucket[field_name] = (
                    max(float(current), float(value))
                    if current is not None
                    else float(value)
                )
            if row.min_rt_ms is not None:
                current_min = bucket.get("min_rt_ms")
                bucket["min_rt_ms"] = (
                    min(float(current_min), float(row.min_rt_ms))
                    if current_min is not None
                    else float(row.min_rt_ms)
                )

        items: list[RunSummaryMetricRow] = []
        for endpoint_name in sorted(merged):
            payload = dict(merged[endpoint_name])
            payload.pop("_avg_weight_total", None)
            payload.pop("_avg_weight_count", None)
            if payload.get("throughput") is not None:
                payload["throughput"] = round(float(payload["throughput"]), 4)
            items.append(RunSummaryMetricRow(**payload))
        return RunSummaryMetricsResponse(items=items)

    def _fetch_agent_summary_metrics_from_all_contexts(
        self,
        run_id: int,
        contexts: list[tuple[str, str]],
    ) -> Optional[RunSummaryMetricsResponse]:
        rows: list[RunSummaryMetricRow] = []
        for ctx in contexts:
            try:
                summary = self._fetch_agent_summary_metrics(ctx)
            except Exception as exc:
                logger.warning(
                    "get_summary_metrics agent failed for run %s host=%s: %s",
                    run_id,
                    ctx[0],
                    exc,
                )
                continue
            if summary and summary.items:
                rows.extend(summary.items)
        if not rows:
            return None
        return self._merge_summary_metric_rows(rows)

    # === Checks (K6 Group-Checks) ===

    def get_checks(self, run_id: int) -> RunChecksResponse:
        """获取 Group-Checks 表。

        K6 场景强依赖；JMeter/Agent 场景在缺少 live/archive 数据时允许返回空数组。

        优先从 run.params.checks 获取种子数据（用于演示/测试）。
        """
        run = self.repo.find_by_id(run_id)
        if not run:
            return RunChecksResponse(items=[])
        self._attach_run_display_fields([run], include_live_runtime_enrichment=False)

        params = run.params or {}

        # 1. 优先从 params.checks 获取种子数据
        seed_checks = self._extract_check_rows(params)
        if seed_checks:
            items: list[RunCheckRow] = []
            for row in seed_checks:
                if isinstance(row, dict):
                    items.append(
                        RunCheckRow(
                            group_name=row.get("group_name", ""),
                            check_name=row.get("check_name", ""),
                            success_rate=row.get("success_rate"),
                        )
                    )
            if items:
                return RunChecksResponse(items=items)

        agent_contexts = self._get_agent_contexts(run)
        status_checks: Optional[RunChecksResponse] = None
        if agent_contexts:
            status_checks = self._fetch_checks_from_status_contexts(
                run_id, agent_contexts
            )

        # 2. 尝试从 agent 获取
        agent_checks: Optional[RunChecksResponse] = None
        if agent_contexts:
            agent_checks = self._fetch_agent_checks_from_all_contexts(
                run_id, agent_contexts
            )

        if self._is_k6_mixed_run(run):
            mixed_checks = self._merge_check_responses(
                status_checks,
                agent_checks,
                self._fetch_prometheus_live_grpc_checks(run, step_seconds=10),
            )
            if mixed_checks and mixed_checks.items:
                return mixed_checks

        if status_checks and status_checks.items:
            return status_checks

        if agent_checks and agent_checks.items:
            return agent_checks

        if self._is_k6_grpc_or_iteration_run(run):
            prom_checks = self._fetch_prometheus_live_grpc_checks(run, step_seconds=10)
            if prom_checks and prom_checks.items:
                return prom_checks

        if self._is_k6_engine(run):
            prom_checks = self._fetch_prometheus_live_grpc_checks(run, step_seconds=10)
            if prom_checks and prom_checks.items:
                return prom_checks

        # 3. 尝试从 S3 归档获取
        checks_s3_uri = params.get("checks_s3") or params.get("metrics_s3")
        if checks_s3_uri:
            try:
                checks = self._fetch_s3_checks(checks_s3_uri)
                if checks and checks.items:
                    return checks
            except Exception as exc:
                logger.warning("get_checks s3 failed for run %s: %s", run_id, exc)

        if run.engine_type != EngineType.K6:
            fallback = self._build_non_k6_checks_fallback(run)
            if fallback and fallback.items:
                return fallback
            return RunChecksResponse(items=[])

        fallback = self._build_fallback_checks(run)
        if fallback and fallback.items:
            return fallback

        return RunChecksResponse(items=[])

    def _build_fallback_checks(self, run: Run) -> Optional[RunChecksResponse]:
        if run.engine_type != EngineType.K6:
            return None
        summary = getattr(
            run, "overview_summary", None
        ) or self._build_run_overview_summary(run, run.params or {})
        k6_summary = (
            (run.params or {}).get("k6_summary")
            if isinstance((run.params or {}).get("k6_summary"), dict)
            else {}
        )
        success_rate = summary.checks_success_rate if summary else None
        if success_rate is None:
            success_rate = self._derive_k6_checks_success_rate(k6_summary)
        if success_rate is None:
            success_rate = run.success_rate
        if success_rate is None:
            return None
        return RunChecksResponse(
            items=[
                RunCheckRow(
                    group_name="overall",
                    check_name="success rate",
                    success_rate=success_rate,
                )
            ]
        )

    def _build_non_k6_checks_fallback(self, run: Run) -> Optional[RunChecksResponse]:
        params = run.params if isinstance(run.params, dict) else {}
        success_rate = run.success_rate
        if success_rate is None and run.error_rate is not None:
            success_rate = max(0.0, min(1.0, 1 - run.error_rate))
        if success_rate is None:
            jtl_summary = (
                params.get("jtl_summary")
                if isinstance(params.get("jtl_summary"), dict)
                else {}
            )
            success_rate = self._parse_seed_ratio(jtl_summary.get("success_rate"))
            if success_rate is None:
                error_rate = self._parse_seed_ratio(jtl_summary.get("error_rate"))
                if error_rate is not None:
                    success_rate = max(0.0, min(1.0, 1 - error_rate))
            if success_rate is None:
                total_requests = self._parse_seed_int(jtl_summary.get("total_requests"))
                failed_requests = self._parse_seed_int(
                    jtl_summary.get("failed_requests")
                )
                successful_requests = self._parse_seed_int(
                    jtl_summary.get("successful_requests")
                )
                if total_requests and total_requests > 0:
                    if successful_requests is not None:
                        success_rate = max(
                            0.0, min(1.0, successful_requests / total_requests)
                        )
                    elif failed_requests is not None:
                        success_rate = max(
                            0.0, min(1.0, 1 - (failed_requests / total_requests))
                        )
        if success_rate is None:
            return None

        summary_rows = self._extract_summary_metric_rows(params)
        endpoint_names = [
            str(row.get("endpoint_name") or "").strip()
            for row in summary_rows
            if isinstance(row, dict)
            and str(row.get("endpoint_name") or "").strip()
            and str(row.get("endpoint_name") or "").strip() != "overall"
        ]
        if not endpoint_names:
            return None

        return RunChecksResponse(
            items=[
                RunCheckRow(
                    group_name=endpoint_name,
                    check_name="success rate",
                    success_rate=success_rate,
                )
                for endpoint_name in endpoint_names
            ]
        )

    def _fetch_agent_checks(self, ctx: tuple[str, str]) -> Optional[RunChecksResponse]:
        """从 agent 获取 checks。"""
        host, token = ctx
        data = self._fetch_agent_json(host, f"/agent/runs/{token}/checks")
        if not data or not data.get("items"):
            return None

        items: list[RunCheckRow] = []
        for row in data.get("items", []):
            items.append(
                RunCheckRow(
                    group_name=row.get("group_name", ""),
                    check_name=row.get("check_name", ""),
                    success_rate=row.get("success_rate"),
                )
            )
        return RunChecksResponse(items=items)

    def _fetch_s3_checks(self, uri: str) -> Optional[RunChecksResponse]:
        """从 S3 归档获取 checks。"""
        bucket, key = s3_utils.parse_s3_uri(uri)
        try:
            raw_bytes = s3_utils.download_bytes(bucket, key)
            if key.endswith(".gz"):
                import gzip

                content = gzip.decompress(raw_bytes).decode("utf-8", errors="ignore")
            else:
                content = raw_bytes.decode("utf-8", errors="ignore")
        except Exception as exc:
            logger.warning("Failed to fetch s3 checks from %s: %s", uri, exc)
            return None

        if not content.strip():
            return None

        items: list[RunCheckRow] = []
        for raw in content.splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            if "check_name" in row or "checks" in row:
                items.append(
                    RunCheckRow(
                        group_name=row.get("group_name", ""),
                        check_name=row.get("check_name", ""),
                        success_rate=row.get("success_rate"),
                    )
                )

        return RunChecksResponse(items=items) if items else None

    def _merge_check_rows(self, rows: list[RunCheckRow]) -> RunChecksResponse:
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (row.group_name, row.check_name)
            bucket = merged.setdefault(
                key,
                {
                    "group_name": row.group_name,
                    "check_name": row.check_name,
                    "_success_total": 0.0,
                    "_success_count": 0,
                },
            )
            if row.success_rate is not None:
                bucket["_success_total"] += float(row.success_rate)
                bucket["_success_count"] += 1
                bucket["success_rate"] = (
                    bucket["_success_total"] / bucket["_success_count"]
                )

        items: list[RunCheckRow] = []
        for key in sorted(merged):
            payload = dict(merged[key])
            payload.pop("_success_total", None)
            payload.pop("_success_count", None)
            items.append(RunCheckRow(**payload))
        return RunChecksResponse(items=items)

    def _merge_check_responses(
        self,
        *responses: Optional[RunChecksResponse],
    ) -> Optional[RunChecksResponse]:
        rows: list[RunCheckRow] = []
        for response in responses:
            if response and response.items:
                rows.extend(response.items)
        if not rows:
            return None
        return self._merge_check_rows(rows)

    def _fetch_checks_from_status_contexts(
        self,
        run_id: int,
        contexts: list[tuple[str, str]],
    ) -> Optional[RunChecksResponse]:
        rows: list[RunCheckRow] = []
        for ctx in contexts:
            try:
                status_payload = self._fetch_agent_status(ctx)
            except Exception as exc:
                logger.warning(
                    "get_checks status failed for run %s host=%s: %s",
                    run_id,
                    ctx[0],
                    exc,
                )
                continue
            k6_summary = (
                status_payload.get("k6_summary")
                if isinstance(status_payload, dict)
                else None
            )
            raw_rows = (
                k6_summary.get("checks") if isinstance(k6_summary, dict) else None
            )
            if not isinstance(raw_rows, list):
                continue
            for row in raw_rows:
                if not isinstance(row, dict):
                    continue
                rows.append(
                    RunCheckRow(
                        group_name=str(row.get("group_name") or "default"),
                        check_name=str(row.get("check_name") or ""),
                        success_rate=self._parse_seed_ratio(row.get("success_rate")),
                    )
                )
        if not rows:
            return None
        return self._merge_check_rows(rows)

    def _fetch_agent_checks_from_all_contexts(
        self,
        run_id: int,
        contexts: list[tuple[str, str]],
    ) -> Optional[RunChecksResponse]:
        rows: list[RunCheckRow] = []
        for ctx in contexts:
            try:
                checks = self._fetch_agent_checks(ctx)
            except Exception as exc:
                logger.warning(
                    "get_checks agent failed for run %s host=%s: %s",
                    run_id,
                    ctx[0],
                    exc,
                )
                continue
            if checks and checks.items:
                rows.extend(checks.items)
        if not rows:
            return None
        return self._merge_check_rows(rows)

    # === Pod Status ===

    def get_pods(self, run_id: int) -> RunPodStatusResponse:
        """获取执行节点/Pod 状态列表。"""
        run = self.repo.find_by_id(run_id)
        if not run:
            return RunPodStatusResponse(items=[])

        agent_contexts = self._get_agent_contexts(run)
        if agent_contexts:
            pods = self._fetch_agent_pods_from_all_contexts(run_id, agent_contexts)
            if pods and pods.items:
                return pods

        # 从 run.params 获取 k8s pod 信息
        params = run.params or {}
        k8s_pods = params.get("k8s_pods") or params.get("pods")
        if k8s_pods and isinstance(k8s_pods, list):
            items: list[RunPodStatus] = []
            for pod in k8s_pods:
                if isinstance(pod, dict):
                    items.append(
                        RunPodStatus(
                            pod_ip=pod.get("pod_ip"),
                            pod_name=pod.get("pod_name"),
                            status=pod.get("status", "unknown"),
                            cluster_name=pod.get("cluster_name"),
                            node_name=pod.get("node_name"),
                            started_at=self._parse_ts(pod.get("started_at")),
                            ended_at=self._parse_ts(pod.get("ended_at")),
                        )
                    )
            if items:
                return RunPodStatusResponse(items=items)

        synthesized_multi_agent = self._build_terminal_multi_agent_pod_statuses(
            run, params
        )
        if synthesized_multi_agent:
            return RunPodStatusResponse(items=synthesized_multi_agent)

        synthesized_single_agent = self._build_terminal_single_agent_pod_status(
            run, params
        )
        if synthesized_single_agent is not None:
            return RunPodStatusResponse(items=[synthesized_single_agent])

        return RunPodStatusResponse(items=[])

    def _fetch_agent_pods(self, ctx: tuple[str, str]) -> Optional[RunPodStatusResponse]:
        """从 agent 获取 pod 状态。"""
        host, token = ctx
        data = self._fetch_agent_json(host, f"/agent/runs/{token}/pods")
        if not data or not data.get("items"):
            return None

        items: list[RunPodStatus] = []
        for row in data.get("items", []):
            items.append(
                RunPodStatus(
                    agent_host=row.get("agent_host") or host,
                    pod_ip=row.get("pod_ip"),
                    pod_name=row.get("pod_name"),
                    status=row.get("status", "unknown"),
                    cluster_name=row.get("cluster_name"),
                    node_name=row.get("node_name"),
                    started_at=self._parse_ts(row.get("started_at")),
                    ended_at=self._parse_ts(row.get("ended_at")),
                )
            )
        return RunPodStatusResponse(items=items)

    def _fetch_agent_pods_from_all_contexts(
        self,
        run_id: int,
        contexts: list[tuple[str, str]],
    ) -> Optional[RunPodStatusResponse]:
        items: list[RunPodStatus] = []
        seen: set[str] = set()
        for ctx in contexts:
            try:
                pods = self._fetch_agent_pods(ctx)
            except Exception as exc:
                logger.warning(
                    "get_pods agent failed for run %s host=%s: %s", run_id, ctx[0], exc
                )
                continue
            if not pods or not pods.items:
                continue
            for item in pods.items:
                dedupe_key = self._build_run_node_identity_key(
                    agent_host=item.agent_host,
                    pod_ip=item.pod_ip,
                    pod_name=item.pod_name,
                )
                if not dedupe_key:
                    dedupe_key = f"agent-{len(items) + 1}"
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                items.append(item)
        return RunPodStatusResponse(items=items) if items else None

    def _build_terminal_multi_agent_pod_statuses(
        self,
        run: Run,
        params: dict[str, Any],
    ) -> list[RunPodStatus]:
        items: list[RunPodStatus] = []
        seen: set[str] = set()
        for entry in self._iter_agent_run_entries(run):
            pod_ip = entry.get("agent_ip")
            host_value = str(entry.get("agent_host") or pod_ip or "").strip()
            pod_name = str(entry.get("pod_name") or "").strip() or (
                host_value.split(":", 1)[0] if host_value else None
            )
            if not pod_name and isinstance(pod_ip, str):
                pod_name = pod_ip
            dedupe_key = self._build_run_node_identity_key(
                agent_host=host_value or None,
                pod_ip=pod_ip if isinstance(pod_ip, str) else None,
                pod_name=pod_name,
            )
            if not dedupe_key:
                dedupe_key = f"agent-{len(items) + 1}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            items.append(
                RunPodStatus(
                    agent_host=host_value or None,
                    pod_ip=pod_ip if isinstance(pod_ip, str) else None,
                    pod_name=pod_name,
                    status=str(entry.get("status") or run.run_status.value).lower(),
                    cluster_name=None,
                    node_name=None,
                    started_at=self._as_utc(run.started_at),
                    ended_at=self._parse_ts(entry.get("ended_at"))
                    or self._as_utc(run.ended_at),
                )
            )
        return items

    @staticmethod
    def _build_run_node_identity_key(
        *,
        agent_host: Optional[str],
        pod_ip: Optional[str],
        pod_name: Optional[str],
    ) -> Optional[str]:
        for raw_value in (agent_host, pod_ip, pod_name):
            if isinstance(raw_value, str):
                normalized = raw_value.strip()
                if normalized:
                    return normalized
        return None

    # === Pod Monitor ===

    def get_pods_monitor(
        self,
        run_id: int,
        step_seconds: int = 10,
    ) -> RunPodMonitorResponse:
        """获取执行节点资源监控。

        优先从 run.params.pod_monitor_series 获取种子数据（用于演示/测试）。
        """
        run = self.repo.find_by_id(run_id)
        if not run:
            return RunPodMonitorResponse(step_seconds=step_seconds, series=[])

        params = run.params or {}
        agent_contexts = self._get_agent_contexts(run)

        if run.run_status == RunStatus.RUNNING and agent_contexts:
            monitor = self._fetch_agent_pods_monitor_from_all_contexts(
                run_id,
                agent_contexts,
                step_seconds,
                params,
            )
            if monitor and monitor.series:
                return monitor

        # 1. 优先从 params.pod_monitor_series 获取种子数据
        seed_series = params.get("pod_monitor_series")
        if seed_series and isinstance(seed_series, list):
            seed_series = self._backfill_seed_pod_monitor_series_agent_hosts(
                seed_series, params
            )
            series: list[RunPodMonitorSeries] = []
            for s in seed_series:
                if not isinstance(s, dict):
                    continue
                metric_str = s.get("metric", "")
                try:
                    metric = RunPodMonitorMetric(metric_str)
                except ValueError:
                    continue

                points: list[MetricPoint] = []
                for p in s.get("points", []):
                    if isinstance(p, dict):
                        ts = self._parse_ts(p.get("ts"))
                        if ts:
                            points.append(MetricPoint(ts=ts, value=p.get("value")))

                series.append(
                    RunPodMonitorSeries(
                        agent_host=s.get("agent_host"),
                        pod_name=s.get("pod_name"),
                        pod_ip=s.get("pod_ip"),
                        metric=metric,
                        unit=s.get("unit", ""),
                        points=points,
                    )
                )
            if series:
                return RunPodMonitorResponse(
                    step_seconds=step_seconds,
                    series=series,
                    summary=self._build_pods_monitor_summary(series, params),
                )

        if agent_contexts:
            monitor = self._fetch_agent_pods_monitor_from_all_contexts(
                run_id,
                agent_contexts,
                step_seconds,
                params,
            )
            if monitor and monitor.series:
                return monitor

        # 3. 返回空 series，不返回 500
        return RunPodMonitorResponse(step_seconds=step_seconds, series=[])

    @staticmethod
    def _backfill_seed_pod_monitor_series_agent_hosts(
        seed_series: list[Any],
        params: dict[str, Any],
    ) -> list[Any]:
        if not seed_series:
            return seed_series
        if any(
            isinstance(item, dict) and item.get("agent_host") for item in seed_series
        ):
            return seed_series

        agent_runs = params.get("agent_runs")
        if not isinstance(agent_runs, list):
            return seed_series
        agent_hosts = [
            str(item.get("agent_host") or "").strip()
            for item in agent_runs
            if isinstance(item, dict) and str(item.get("agent_host") or "").strip()
        ]
        if len(agent_hosts) <= 1:
            return seed_series

        group_keys: list[str] = []
        grouped_indexes: dict[str, list[int]] = {}
        for index, item in enumerate(seed_series):
            if not isinstance(item, dict):
                continue
            points = item.get("points")
            if not isinstance(points, list) or not points:
                continue
            first_point = points[0]
            if not isinstance(first_point, dict):
                continue
            ts_value = str(first_point.get("ts") or "").strip()
            if not ts_value:
                continue
            if ts_value not in grouped_indexes:
                group_keys.append(ts_value)
                grouped_indexes[ts_value] = []
            grouped_indexes[ts_value].append(index)

        if len(group_keys) != len(agent_hosts):
            return seed_series

        enriched = [
            dict(item) if isinstance(item, dict) else item for item in seed_series
        ]
        for host, group_key in zip(agent_hosts, group_keys):
            for item_index in grouped_indexes.get(group_key, []):
                item = enriched[item_index]
                if isinstance(item, dict):
                    item["agent_host"] = host
        return enriched

    @staticmethod
    def _build_pods_monitor_summary(
        series: list[RunPodMonitorSeries],
        params: Optional[dict[str, Any]] = None,
    ) -> RunPodMonitorSummary:
        def _metric_values(metric: RunPodMonitorMetric) -> list[float]:
            values: list[float] = []
            for item in series:
                if item.metric != metric:
                    continue
                values.extend(
                    float(point.value)
                    for point in item.points
                    if isinstance(point.value, (int, float))
                )
            return values

        def _latest_metric_value(metric: RunPodMonitorMetric) -> Optional[float]:
            latest_point: Optional[MetricPoint] = None
            for item in series:
                if item.metric != metric:
                    continue
                for point in item.points:
                    if not isinstance(point.value, (int, float)):
                        continue
                    if latest_point is None or point.ts > latest_point.ts:
                        latest_point = point
            return (
                float(latest_point.value)
                if latest_point and latest_point.value is not None
                else None
            )

        observed_pods = {
            identity_key
            for identity_key in (
                RunService._build_run_node_identity_key(
                    agent_host=item.agent_host,
                    pod_ip=item.pod_ip,
                    pod_name=item.pod_name,
                )
                for item in series
            )
            if identity_key
        }
        cpu_values = _metric_values(RunPodMonitorMetric.CPU_USAGE_PERCENT)
        memory_values = _metric_values(RunPodMonitorMetric.MEMORY_USAGE_PERCENT)
        socket_values = _metric_values(RunPodMonitorMetric.SOCKET_COUNT)
        network_rx_values = _metric_values(RunPodMonitorMetric.NETWORK_RX_BYTES)
        network_tx_values = _metric_values(RunPodMonitorMetric.NETWORK_TX_BYTES)
        disk_usage_values = _metric_values(RunPodMonitorMetric.DISK_USAGE_PERCENT)
        disk_used_values = _metric_values(RunPodMonitorMetric.DISK_USED_BYTES)
        network_rx_packet_values = _metric_values(
            RunPodMonitorMetric.NETWORK_RX_PACKETS
        )
        network_tx_packet_values = _metric_values(
            RunPodMonitorMetric.NETWORK_TX_PACKETS
        )

        cpu_peak = max(cpu_values) if cpu_values else None
        memory_peak = max(memory_values) if memory_values else None
        socket_peak = max(socket_values) if socket_values else None
        cpu_load_latest = _latest_metric_value(RunPodMonitorMetric.CPU_LOAD)
        network_rx_peak = max(network_rx_values) if network_rx_values else None
        network_tx_peak = max(network_tx_values) if network_tx_values else None
        disk_usage_peak = max(disk_usage_values) if disk_usage_values else None
        disk_used_peak = max(disk_used_values) if disk_used_values else None
        disk_total_latest = _latest_metric_value(RunPodMonitorMetric.DISK_TOTAL_BYTES)
        network_rx_packet_peak = (
            max(network_rx_packet_values) if network_rx_packet_values else None
        )
        network_tx_packet_peak = (
            max(network_tx_packet_values) if network_tx_packet_values else None
        )
        has_non_empty_series = bool(series)

        cpu_summary_label = RunService._format_cpu_monitor_summary(
            cpu_peak, cpu_load_latest
        )
        if cpu_summary_label is None and has_non_empty_series:
            cpu_summary_label = "CPU 未上报"

        memory_summary_label = RunService._format_memory_monitor_summary(memory_peak)
        if memory_summary_label is None:
            memory_summary_label = RunService._format_memory_monitor_fallback(
                disk_usage_peak=disk_usage_peak,
                disk_used_peak=disk_used_peak,
                disk_total_latest=disk_total_latest,
                has_series=has_non_empty_series,
            )

        network_summary_label = RunService._format_network_monitor_summary(
            network_rx_peak, network_tx_peak
        )
        if network_summary_label is None:
            network_summary_label = RunService._format_network_packet_monitor_summary(
                network_rx_packet_peak=network_rx_packet_peak,
                network_tx_packet_peak=network_tx_packet_peak,
                has_series=has_non_empty_series,
            )

        runtime_summary_label = RunService._format_runtime_monitor_summary(socket_peak)
        if runtime_summary_label is None:
            runtime_summary_label = RunService._format_runtime_monitor_fallback(
                disk_used_peak=disk_used_peak,
                disk_total_latest=disk_total_latest,
                has_series=has_non_empty_series,
            )

        runtime_kind = RunService._infer_pods_monitor_runtime_kind(params or {}, series)
        resource_scope_label = RunService._build_pods_monitor_scope_label(runtime_kind)
        if runtime_kind == "host" and network_summary_label == "字节未上报":
            network_summary_label = "Host 口径留空"

        return RunPodMonitorSummary(
            observed_pod_total=len(observed_pods),
            runtime_kind=runtime_kind,
            resource_scope_label=resource_scope_label,
            cpu_usage_peak_percent=cpu_peak,
            memory_usage_peak_percent=memory_peak,
            socket_peak=socket_peak,
            cpu_load_latest=cpu_load_latest,
            network_rx_peak_bytes=network_rx_peak,
            network_tx_peak_bytes=network_tx_peak,
            cpu_summary_label=cpu_summary_label,
            memory_summary_label=memory_summary_label,
            network_summary_label=network_summary_label,
            runtime_summary_label=runtime_summary_label,
        )

    @staticmethod
    def _infer_pods_monitor_runtime_kind(
        params: dict[str, Any],
        series: list[RunPodMonitorSeries],
    ) -> Optional[str]:
        runtime_kind = str(params.get("agent_runtime_kind") or "").strip().lower()
        if runtime_kind:
            return runtime_kind

        metadata = params.get("agent_metadata")
        if isinstance(metadata, dict):
            runtime_kind = str(metadata.get("runtime_kind") or "").strip().lower()
            if runtime_kind:
                return runtime_kind

        def _looks_like_docker_pod_name(raw_value: Optional[str]) -> bool:
            if not isinstance(raw_value, str):
                return False
            value = raw_value.strip().lower()
            if not value:
                return False
            if len(value) == 12 and all(ch in "0123456789abcdef" for ch in value):
                return True
            return value.startswith(("docker-", "ptp-agent-", "ptp-agent_"))

        observed_pod_ips = {
            str(item.pod_ip or "").strip()
            for item in series
            if isinstance(item.pod_ip, str) and item.pod_ip.strip()
        }
        if "127.0.0.1" in observed_pod_ips or "::1" in observed_pod_ips:
            return "host"

        observed_pod_names = [
            str(item.pod_name or "").strip()
            for item in series
            if isinstance(item.pod_name, str) and item.pod_name.strip()
        ]
        if any(_looks_like_docker_pod_name(name) for name in observed_pod_names):
            return "docker"
        if any("." in name for name in observed_pod_names):
            return "host"
        return None

    @staticmethod
    def _build_pods_monitor_scope_label(runtime_kind: Optional[str]) -> Optional[str]:
        normalized = str(runtime_kind or "").strip().lower()
        if normalized == "host":
            return "Host / EC2 resource scope: process-tree metrics with host-level capacity metrics"
        if normalized in {"docker", "k8s"}:
            return "Docker / K8S container resource scope"
        return None

    def _fetch_agent_pods_monitor(
        self, ctx: tuple[str, str], step_seconds: int
    ) -> Optional[RunPodMonitorResponse]:
        """从 agent 获取 pod 监控数据。"""
        host, token = ctx
        data = self._fetch_agent_json(
            host,
            f"/agent/runs/{token}/pods/monitor",
            params={"step_seconds": step_seconds},
        )
        if not data or not data.get("series"):
            return None

        series: list[RunPodMonitorSeries] = []
        for s in data.get("series", []):
            metric_str = s.get("metric", "")
            try:
                metric = RunPodMonitorMetric(metric_str)
            except ValueError:
                continue

            points: list[MetricPoint] = []
            for p in s.get("points", []):
                ts = self._parse_ts(p.get("ts"))
                if not ts:
                    continue
                points.append(MetricPoint(ts=ts, value=p.get("value")))

            if points:
                series.append(
                    RunPodMonitorSeries(
                        agent_host=s.get("agent_host") or host,
                        pod_name=s.get("pod_name"),
                        pod_ip=s.get("pod_ip"),
                        metric=metric,
                        unit=s.get("unit", ""),
                        points=points,
                    )
                )

        return RunPodMonitorResponse(
            step_seconds=int(data.get("step_seconds") or step_seconds),
            series=series,
            summary=self._build_pods_monitor_summary(series),
        )

    def _fetch_agent_pods_monitor_from_all_contexts(
        self,
        run_id: int,
        contexts: list[tuple[str, str]],
        step_seconds: int,
        params: Optional[dict[str, Any]] = None,
    ) -> Optional[RunPodMonitorResponse]:
        series: list[RunPodMonitorSeries] = []
        for ctx in contexts:
            try:
                monitor = self._fetch_agent_pods_monitor(ctx, step_seconds)
            except Exception as exc:
                logger.warning(
                    "get_pods_monitor agent failed for run %s host=%s: %s",
                    run_id,
                    ctx[0],
                    exc,
                )
                continue
            if not monitor or not monitor.series:
                continue
            series.extend(monitor.series)
        if not series:
            return None
        return RunPodMonitorResponse(
            step_seconds=step_seconds,
            series=series,
            summary=self._build_pods_monitor_summary(series, params),
        )

    # === Dashboards ===

    def get_dashboards(self, run_id: int) -> RunDashboardsResponse:
        """获取关联监控看板入口列表。"""
        run = self.repo.find_by_id(run_id)
        if not run:
            return RunDashboardsResponse(items=[])

        items: list[RunDashboardLink] = []
        params = run.params or {}

        # 引擎 Grafana
        engine_dashboard_url = self._build_engine_grafana_url(run)
        if engine_dashboard_url:
            items.append(
                RunDashboardLink(
                    dashboard_type=RunDashboardType.ENGINE_GRAFANA,
                    title=f"{run.engine_type.value.upper()} 监控看板",
                    url=engine_dashboard_url,
                    embed_mode=self._resolve_dashboard_embed_mode(
                        params, "engine_grafana_embed_mode", default="iframe"
                    ),
                )
            )

        # Pod Grafana（优先使用 _build_pod_grafana_url 构建默认 URL）
        pod_grafana_url = self._build_pod_grafana_url(run)
        pod_grafana_url = self._normalize_pod_grafana_url_for_display(pod_grafana_url)
        if pod_grafana_url:
            items.append(
                RunDashboardLink(
                    dashboard_type=RunDashboardType.POD_GRAFANA,
                    title="执行节点监控",
                    url=pod_grafana_url,
                    embed_mode=self._resolve_dashboard_embed_mode(
                        params, "pod_grafana_embed_mode", default="iframe"
                    ),
                )
            )

        self._append_param_dashboards(
            items=items,
            params=params,
            run=run,
            source_key="related_monitors",
            dashboard_type=RunDashboardType.RELATED_MONITOR,
            default_title_prefix="关联监控",
            default_embed_mode="new_tab",
        )
        self._append_param_dashboards(
            items=items,
            params=params,
            run=run,
            source_key="topology_dashboards",
            dashboard_type=RunDashboardType.TOPOLOGY,
            default_title_prefix="链路拓扑图",
            default_embed_mode="new_tab",
        )
        self._append_trace_link_dashboard(items=items, params=params)
        self._append_param_dashboards(
            items=items,
            params=params,
            run=run,
            source_key="server_config_dashboards",
            dashboard_type=RunDashboardType.SERVER_CONFIG,
            default_title_prefix="服务端配置量",
            default_embed_mode="new_tab",
        )

        return RunDashboardsResponse(
            items=items, summary=self._build_dashboard_summary(items)
        )

    @staticmethod
    def _build_dashboard_summary(items: list[RunDashboardLink]) -> RunDashboardSummary:
        has_engine = any(
            item.dashboard_type == RunDashboardType.ENGINE_GRAFANA for item in items
        )
        has_pod = any(
            item.dashboard_type == RunDashboardType.POD_GRAFANA for item in items
        )
        related_total = sum(
            1
            for item in items
            if item.dashboard_type == RunDashboardType.RELATED_MONITOR
        )
        topology_total = sum(
            1 for item in items if item.dashboard_type == RunDashboardType.TOPOLOGY
        )
        config_total = sum(
            1 for item in items if item.dashboard_type == RunDashboardType.SERVER_CONFIG
        )
        return RunDashboardSummary(
            has_engine_grafana=has_engine,
            has_pod_grafana=has_pod,
            related_monitor_total=related_total,
            topology_total=topology_total,
            server_config_total=config_total,
            total_dashboard_count=len(items),
            engine_grafana_label="已接通" if has_engine else "未配置",
            pod_grafana_label="已接通" if has_pod else "未配置",
            related_monitor_label=(
                f"{related_total} 个业务大盘" if related_total > 0 else "无业务大盘"
            ),
            topology_label=(
                f"{topology_total} 个拓扑视图" if topology_total > 0 else "无拓扑视图"
            ),
            server_config_label=(
                f"{config_total} 个配置视图" if config_total > 0 else "无配置视图"
            ),
        )

    @staticmethod
    def _format_cpu_monitor_summary(
        cpu_peak: Optional[float], cpu_load_latest: Optional[float]
    ) -> Optional[str]:
        parts: list[str] = []
        if cpu_peak is not None:
            parts.append(f"峰值 {cpu_peak:.1f}%")
        if cpu_load_latest is not None:
            parts.append(f"Load {cpu_load_latest:.2f}")
        return " / ".join(parts) if parts else None

    @staticmethod
    def _format_memory_monitor_summary(memory_peak: Optional[float]) -> Optional[str]:
        if memory_peak is None:
            return None
        return f"峰值 {memory_peak:.1f}%"

    @staticmethod
    def _format_memory_monitor_fallback(
        disk_usage_peak: Optional[float],
        disk_used_peak: Optional[float],
        disk_total_latest: Optional[float],
        has_series: bool,
    ) -> Optional[str]:
        if disk_usage_peak is not None:
            return f"内存未上报 / 磁盘峰值 {disk_usage_peak:.1f}%"
        if disk_used_peak is not None or disk_total_latest is not None:
            return (
                "内存未上报 / 磁盘 "
                f"{RunService._format_used_total_short(disk_used_peak, disk_total_latest)}"
            )
        if has_series:
            return "内存未上报"
        return None

    @staticmethod
    def _format_network_monitor_summary(
        network_rx_peak: Optional[float], network_tx_peak: Optional[float]
    ) -> Optional[str]:
        parts: list[str] = []
        if network_rx_peak is not None:
            parts.append(f"RX {RunService._format_bytes_short(network_rx_peak)}")
        if network_tx_peak is not None:
            parts.append(f"TX {RunService._format_bytes_short(network_tx_peak)}")
        return " / ".join(parts) if parts else None

    @staticmethod
    def _format_network_packet_monitor_summary(
        network_rx_packet_peak: Optional[float],
        network_tx_packet_peak: Optional[float],
        has_series: bool,
    ) -> Optional[str]:
        parts: list[str] = []
        if network_rx_packet_peak is not None:
            parts.append(f"RX {network_rx_packet_peak:.0f}")
        if network_tx_packet_peak is not None:
            parts.append(f"TX {network_tx_packet_peak:.0f}")
        if parts:
            return "字节未上报 / 包量 " + " / ".join(parts)
        if has_series:
            return "字节未上报"
        return None

    @staticmethod
    def _format_runtime_monitor_summary(socket_peak: Optional[float]) -> Optional[str]:
        if socket_peak is None:
            return None
        return f"Socket {socket_peak:.0f}"

    @staticmethod
    def _format_runtime_monitor_fallback(
        disk_used_peak: Optional[float],
        disk_total_latest: Optional[float],
        has_series: bool,
    ) -> Optional[str]:
        if disk_used_peak is not None or disk_total_latest is not None:
            return (
                "Socket 未上报 / 磁盘 "
                f"{RunService._format_used_total_short(disk_used_peak, disk_total_latest)}"
            )
        if has_series:
            return "Socket 未上报"
        return None

    @staticmethod
    def _format_bytes_short(value: float) -> str:
        if abs(value) >= 1024 * 1024:
            return f"{value / (1024 * 1024):.1f} MiB"
        if abs(value) >= 1024:
            return f"{value / 1024:.1f} KiB"
        return f"{value:.0f} B"

    @staticmethod
    def _format_used_total_short(used: Optional[float], total: Optional[float]) -> str:
        parts: list[str] = []
        if used is not None:
            parts.append(RunService._format_bytes_short(used))
        if total is not None:
            parts.append(RunService._format_bytes_short(total))
        if len(parts) == 2:
            return f"{parts[0]} / {parts[1]}"
        if parts:
            return parts[0]
        return "已观测"

    def _append_param_dashboards(
        self,
        items: list[RunDashboardLink],
        params: dict,
        run: Run | None,
        source_key: str,
        dashboard_type: RunDashboardType,
        default_title_prefix: str,
        default_embed_mode: str = "new_tab",
    ) -> None:
        dashboards = params.get(source_key) or []
        if source_key == "related_monitors":
            dashboards = self._normalize_public_demo_related_monitors(
                dashboards, params
            )
        if not isinstance(dashboards, list):
            return
        for index, dashboard in enumerate(dashboards):
            if not isinstance(dashboard, dict):
                continue
            raw_url = dashboard.get("url") or dashboard.get("link")
            if not isinstance(raw_url, str) or not raw_url.strip():
                continue
            url = raw_url.strip()
            if dashboard_type == RunDashboardType.TOPOLOGY:
                url = self._normalize_public_demo_topology_url(url, params)
            elif dashboard_type == RunDashboardType.RELATED_MONITOR and run is not None:
                url = self._append_run_window_to_related_grafana_url(
                    url, run=run, dashboard=dashboard
                )
            raw_title = dashboard.get("title") or dashboard.get("name")
            title = (
                raw_title.strip()
                if isinstance(raw_title, str) and raw_title.strip()
                else f"{default_title_prefix} {index + 1}"
            )
            items.append(
                RunDashboardLink(
                    dashboard_type=dashboard_type,
                    title=title,
                    url=url,
                    embed_mode=self._resolve_dashboard_embed_mode(
                        dashboard, "embed_mode", default=default_embed_mode
                    ),
                )
            )

    def _append_run_window_to_related_grafana_url(
        self, raw_url: str, *, run: Run, dashboard: dict
    ) -> str:
        if not self._is_related_grafana_dashboard_url(raw_url, dashboard):
            return raw_url
        try:
            parts = urlsplit(raw_url)
            query_pairs = [
                (key, value)
                for key, value in parse_qsl(parts.query, keep_blank_values=True)
                if key not in {"from", "to"}
            ]
        except Exception:
            return raw_url
        start_param, end_param = self._build_dashboard_window_params(run)
        query_pairs.extend((("from", start_param), ("to", end_param)))
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urlencode(query_pairs),
                parts.fragment,
            )
        )

    @staticmethod
    def _is_related_grafana_dashboard_url(raw_url: str, dashboard: dict) -> bool:
        raw_kind = dashboard.get("kind") or dashboard.get("provider")
        if isinstance(raw_kind, str) and raw_kind.strip().lower() == "grafana":
            return True
        try:
            parts = urlsplit(raw_url)
        except Exception:
            return False
        host = parts.netloc.lower()
        path = parts.path.lower()
        return "grafana" in host or path.startswith("/d/")

    def _normalize_pod_grafana_url_for_display(
        self, raw_url: Optional[str]
    ) -> Optional[str]:
        if not isinstance(raw_url, str) or not raw_url.strip():
            return raw_url
        try:
            parts = urlsplit(raw_url.strip())
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
        except Exception:
            return raw_url
        compose_service = str(query.get("var-compose_service") or "").strip()
        if compose_service.startswith("ptp-agent"):
            query["var-compose_service"] = (
                self.POD_GRAFANA_DEFAULT_COMPOSE_SERVICE_REGEX
            )
        normalized_query = urlencode(query)
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, normalized_query, parts.fragment)
        )

    def _normalize_public_demo_related_monitors(
        self, dashboards: Any, params: dict
    ) -> Any:
        if not isinstance(dashboards, list):
            return dashboards
        is_public_demo = str(params.get("demo_seed_slug") or "").startswith(
            "openloadhub-demo-"
        ) or any(
            isinstance(item, str) and "openloadhub-demo-target" in item
            for item in (params.get("related_apps") or [])
        )
        normalized: list[Any] = []
        has_demo_target_monitor = False
        for dashboard in dashboards:
            if not isinstance(dashboard, dict):
                normalized.append(dashboard)
                continue
            title = str(dashboard.get("title") or dashboard.get("name") or "")
            url = str(dashboard.get("url") or dashboard.get("link") or "")
            lower_title = title.lower()
            if (
                is_public_demo
                and (
                    "openloadhub demo target" in lower_title
                    or "demo-target-dashboard" in url
                )
            ):
                normalized.append(self._build_public_demo_target_monitor())
                has_demo_target_monitor = True
                continue
            patched = deepcopy(dashboard)
            if is_public_demo and (
                "demo-target-dashboard" in url
                or "demo-target-dashboard" in url
                or "redis-dashboard" in url
                or "mysql-dashboard" in url
            ):
                patched["embed_mode"] = "new_tab"
            normalized.append(patched)
        if (
            is_public_demo
            and not has_demo_target_monitor
            and not any(
                isinstance(item, dict)
                and "demo-target-dashboard"
                in str(item.get("url") or item.get("link") or "")
                for item in normalized
            )
        ):
            normalized.insert(0, self._build_public_demo_target_monitor())
        return normalized

    def _build_public_demo_target_monitor(self) -> dict[str, Any]:
        grafana_base = self._resolve_grafana_public_base_url()
        if not grafana_base:
            grafana_base = "http://127.0.0.1:13001"
        return {
            "title": "OpenLoadHub Demo Target - Demo Target",
            "url": (
                f"{grafana_base}{self.DEMO_TARGET_DASHBOARD_PATH}"
                "?orgId=1&var-job=demo-target&var-target_instance=.%2A&var-instance=.%2A&refresh=15s"
            ),
            "kind": "grafana",
            "embed_mode": "new_tab",
            "description": "Demo Target service dashboard for HTTP/gRPC demo target",
        }

    @staticmethod
    def _normalize_public_demo_topology_url(raw_url: str, params: dict) -> str:
        is_public_demo = str(params.get("demo_seed_slug") or "").startswith(
            "openloadhub-demo-"
        ) or any(
            isinstance(item, str) and "openloadhub-demo-target" in item
            for item in (params.get("related_apps") or [])
        )
        try:
            parts = urlsplit(raw_url)
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
        except Exception:
            return raw_url
        if "service" in query and query.get("service") != "openloadhub-demo-target":
            return raw_url
        host = parts.netloc.lower()
        if "18090" not in host and "skywalking" not in raw_url.lower():
            return raw_url
        if not is_public_demo:
            return raw_url
        query["provider"] = query.get("provider") or "skywalking"
        query["service"] = "openloadhub-demo-target"
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path or "/",
                urlencode(query),
                parts.fragment,
            )
        )

    def _append_trace_link_dashboard(
        self, items: list[RunDashboardLink], params: dict
    ) -> None:
        raw_trace_link = params.get("trace_link")
        if not isinstance(raw_trace_link, str) or not raw_trace_link.strip():
            return
        items.append(
            RunDashboardLink(
                dashboard_type=RunDashboardType.TOPOLOGY,
                title="Trace 链路入口",
                url=raw_trace_link.strip(),
                embed_mode=self._resolve_dashboard_embed_mode(
                    params, "trace_link_embed_mode", default="new_tab"
                ),
            )
        )

    @classmethod
    def _inject_observability_snapshot(
        cls, params: dict[str, Any], properties: dict[str, Any]
    ) -> None:
        if not isinstance(properties, dict):
            return

        for key in ("topology_dashboards", "server_config_dashboards"):
            dashboards = cls._normalize_dashboard_snapshot_list(properties.get(key))
            if dashboards and key not in params:
                params[key] = dashboards

        related_monitors = cls._normalize_dashboard_snapshot_list(
            properties.get("related_monitors")
        )
        if not related_monitors:
            monitor_link_snapshot = cls._build_monitor_link_snapshot(properties)
            if monitor_link_snapshot:
                related_monitors = [monitor_link_snapshot]
        if related_monitors and "related_monitors" not in params:
            params["related_monitors"] = related_monitors

        for key in (
            "trace_link",
            "alert_subscriptions",
            "alert_policies",
            "observability_queries",
            "query_templates",
        ):
            value = properties.get(key)
            if cls._has_observability_snapshot_value(value) and key not in params:
                params[key] = deepcopy(value)

        related_apps = cls._normalize_string_list_snapshot(
            properties.get("related_apps")
        )
        app_snapshot = related_apps
        if app_snapshot:
            if "related_apps" not in params:
                params["related_apps"] = deepcopy(app_snapshot)

    @staticmethod
    def _normalize_dashboard_snapshot_list(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []

        dashboards: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            raw_url = item.get("url") or item.get("link")
            if not isinstance(raw_url, str) or not raw_url.strip():
                continue
            dashboard = deepcopy(item)
            dashboard["url"] = raw_url.strip()
            raw_title = dashboard.get("title") or dashboard.get("name")
            if isinstance(raw_title, str) and raw_title.strip():
                dashboard["title"] = raw_title.strip()
            if "link" in dashboard and dashboard["link"] == dashboard["url"]:
                dashboard.pop("link", None)
            dashboards.append(dashboard)
        return dashboards

    @staticmethod
    def _build_monitor_link_snapshot(
        properties: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        raw_url = properties.get("monitor_link") or properties.get(
            "monitor_dashboard_url"
        )
        if not isinstance(raw_url, str) or not raw_url.strip():
            return None
        return {
            "title": "关联监控 1",
            "url": raw_url.strip(),
            "kind": "grafana",
            "embed_mode": "new_tab",
        }

    @staticmethod
    def _normalize_string_list_snapshot(value: Any) -> list[str]:
        if isinstance(value, list):
            return [
                item.strip() for item in value if isinstance(item, str) and item.strip()
            ]
        if isinstance(value, str) and value.strip():
            return [item.strip() for item in value.split(",") if item.strip()]
        return []

    @staticmethod
    def _has_observability_snapshot_value(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, dict)):
            return bool(value)
        return True

    @staticmethod
    def _resolve_dashboard_embed_mode(
        source: dict, key: str, default: str = "iframe"
    ) -> str:
        value = str(source.get(key, default) or default).strip().lower()
        if value in {"iframe", "new_tab"}:
            return value
        return default

    def _build_dashboard_window_timestamps(self, run: Run) -> tuple[int, int]:
        started_at = self._as_utc(run.started_at) or datetime.now(timezone.utc)
        ended_at = self._as_utc(run.ended_at) or datetime.now(timezone.utc)

        start_ts = (
            int(started_at.timestamp() * 1000) - self.DASHBOARD_WINDOW_START_PADDING_MS
        )
        # Historical runs can miss the first Prometheus sample when Grafana only extends
        # to ended_at + 10s. Keep a wider right edge so a 15s scrape plus small jitter
        # still stays inside the default dashboard URL window.
        end_ts = int(ended_at.timestamp() * 1000) + self.DASHBOARD_WINDOW_END_PADDING_MS
        return start_ts, end_ts

    def _build_dashboard_window_params(self, run: Run) -> tuple[str, str]:
        if self._as_utc(run.ended_at) is None:
            return self.DASHBOARD_LIVE_RELATIVE_FROM, self.DASHBOARD_LIVE_RELATIVE_TO

        start_ts, end_ts = self._build_dashboard_window_timestamps(run)
        return str(start_ts), str(end_ts)

    def _build_engine_grafana_url(self, run: Run) -> Optional[str]:
        """构建引擎 Grafana 看板 URL。

        Dashboard 方案说明：

        JMeter Dashboard (UID: jmeter-load-test-influx):
        - 当前 canonical 仍使用 `jmeter-load-test-influx` UID，JMX 注入使用 JMeter 内置 `InfluxdbBackendListenerClient`
        - 数据源为 `InfluxDB`，默认按 `application=run_id` 过滤内置 `jmeter` measurement
        - 历史 `jmeter-dashboard` 保留为 Prometheus 兼容存档，不再作为 RunDetail 默认 engine dashboard

        K6 HTTP Dashboard (UID: k6-prometheus-ptp):
        - 基于 Grafana 官方 k6 Prometheus dashboard (ID: 19665) 适配
        - 适配点：testid -> recordId 标签，匹配 ptp-agent 配置
        - 使用 k6_http_* 原生 metrics（通过 Prometheus remote write 输出）

        K6 WebSocket Dashboard (UID: 21Ev3D0Ik):
        - 使用当前平台已验证可稳定产出的通用 k6 指标
        - 优先覆盖 VUs / checks / iteration duration / data in-out 等运行态观测

        K6 Kafka Dashboard (UID: usA2Xd_4z):
        - 同样使用通用 k6 指标承载 Kafka run 的独立 Grafana 入口
        - 当前不强依赖 topic/offset 等扩展指标，避免把未稳定落地的面板硬编码进合同
        """
        params = run.params or {}
        # 优先使用参数传入的 URL
        if params.get("engine_grafana_url"):
            return params["engine_grafana_url"]

        # 根据引擎类型构建默认 URL
        grafana_base = self._resolve_grafana_public_base_url()
        if not grafana_base:
            return None

        start_param, end_param = self._build_dashboard_window_params(run)
        grafana_org_id = os.getenv("GRAFANA_ORG_ID", settings.GRAFANA_ORG_ID)

        if run.engine_type == EngineType.K6:
            protocol = self._resolve_run_protocol(run) or "http"
            k6_dashboard_map = {
                "http": os.getenv(
                    "K6_HTTP_DASHBOARD_UID", settings.K6_HTTP_DASHBOARD_UID
                ),
                "grpc": os.getenv(
                    "K6_GRPC_DASHBOARD_UID", settings.K6_GRPC_DASHBOARD_UID
                ),
                "websocket": os.getenv(
                    "K6_WS_DASHBOARD_UID", settings.K6_WS_DASHBOARD_UID
                ),
                "kafka": os.getenv(
                    "K6_KAFKA_DASHBOARD_UID", settings.K6_KAFKA_DASHBOARD_UID
                ),
                "browser": os.getenv(
                    "K6_BROWSER_DASHBOARD_UID", settings.K6_BROWSER_DASHBOARD_UID
                ),
            }
            dashboard_uid = k6_dashboard_map.get(protocol, k6_dashboard_map["http"])
            dashboard_path = self._build_grafana_dashboard_path(dashboard_uid)
            return f"{grafana_base}{dashboard_path}?var-recordId={run.run_id}&from={start_param}&to={end_param}&refresh=10s&orgId={grafana_org_id}"

        elif run.engine_type == EngineType.JMETER:
            jmeter_dashboard_uid = os.getenv(
                "JMETER_DASHBOARD_UID", settings.JMETER_DASHBOARD_UID
            )
            dashboard_path = self._build_grafana_dashboard_path(jmeter_dashboard_uid)
            return f"{grafana_base}{dashboard_path}?var-run_id={run.run_id}&var-aggregation=5&from={start_param}&to={end_param}&refresh=5s&orgId={grafana_org_id}"

        return None

    def _build_grafana_dashboard_path(self, dashboard_uid: str) -> str:
        slug = self.GRAFANA_DASHBOARD_SLUGS.get(dashboard_uid, dashboard_uid)
        return f"/d/{dashboard_uid}/{slug}"

    def _resolve_pod_dashboard_uid(self, run: Run) -> str:
        runtime_kind = self._infer_pods_monitor_runtime_kind(run.params or {}, [])
        if runtime_kind == "host":
            return os.getenv("POD_HOST_DASHBOARD_UID", settings.POD_HOST_DASHBOARD_UID)
        return os.getenv("POD_DASHBOARD_UID", settings.POD_DASHBOARD_UID)

    def _build_pod_grafana_url(self, run: Run) -> Optional[str]:
        """构建执行节点 Grafana 看板 URL。

        URL 构建规则：
        - 模板：{GRAFANA_BASE_URL}/d/{POD_DASHBOARD_UID}?var-run_id={run_id}&var-compose_service={service}&var-container_hint={hint}&from={start_ts}&to={end_ts}&refresh=5s
        - dashboard 内部会再用 `^run_id-.*` 精确派生 `run_token`，避免 `.*run_id.*` 的串号误匹配
        - `compose_service` 默认走可扩展 regex（当前默认 `ptp-agent.*`），用于在多 agent 场景下消费共享 cAdvisor 容器级资源指标
        - `container_hint` 默认为 `__no_container_hint__`；当 run 已带 pod/container 前缀时，dashboard 可在 cAdvisor 仅暴露 `id=/docker/<container-id>` 的场景下回退匹配
        - embed_mode：iframe（前端可嵌入展示）

        优先级：
        1. params.pod_grafana_url（显式指定）
        2. 基于 GRAFANA_BASE_URL 和 POD_DASHBOARD_UID 构建默认 URL

        当前方案局限（pod-monitor-dashboard）：
        - 当前已补第一阶段 cAdvisor 容器级资源指标（CPU / Memory / Network），默认过滤 `ptp-agent`
        - 仍不包含宿主机级 / K8S 节点级 / 磁盘级完整资源监控
        - 若需完整资源监控，仍需继续引入 node-exporter 或补充 K8S collector
        - 后续可在 ptp-agent 中增加 ptp_agent_* 资源指标推送
        """
        params = run.params or {}
        # 优先使用参数传入的 URL
        if params.get("pod_grafana_url"):
            return params["pod_grafana_url"]

        # 构建默认 URL
        grafana_base = self._resolve_grafana_public_base_url()
        if not grafana_base:
            return None

        pod_dashboard_uid = self._resolve_pod_dashboard_uid(run)
        start_param, end_param = self._build_dashboard_window_params(run)

        compose_service = (
            params.get("pod_grafana_compose_service")
            or os.getenv("POD_GRAFANA_COMPOSE_SERVICE_REGEX")
            or self.POD_GRAFANA_DEFAULT_COMPOSE_SERVICE_REGEX
        )
        container_hint = self._resolve_pod_grafana_container_hint(params)
        query = urlencode(
            {
                "var-run_id": run.run_id,
                "var-compose_service": compose_service,
                "var-container_hint": container_hint,
                "from": start_param,
                "to": end_param,
                "refresh": "5s",
            }
        )
        dashboard_path = self._build_grafana_dashboard_path(pod_dashboard_uid)
        grafana_org_id = os.getenv("GRAFANA_ORG_ID", settings.GRAFANA_ORG_ID)
        return f"{grafana_base}{dashboard_path}?{query}&orgId={grafana_org_id}"

    @staticmethod
    def _resolve_grafana_public_base_url() -> Optional[str]:
        public_base = str(
            getattr(settings, "GRAFANA_PUBLIC_BASE_URL", "") or ""
        ).strip()
        if public_base:
            return public_base.rstrip("/")

        internal_base = str(settings.GRAFANA_BASE_URL or "").strip()
        if not internal_base:
            return None
        return internal_base.rstrip("/")

    def _resolve_pod_grafana_container_hint(self, params: dict) -> str:
        explicit_hint = params.get("pod_grafana_container_hint")
        if isinstance(explicit_hint, str):
            normalized = self._normalize_pod_grafana_container_hint(explicit_hint)
            if normalized:
                return normalized

        for key in ("pod_name", "agent_name", "agent_pod_name"):
            value = params.get(key)
            if isinstance(value, str):
                normalized = self._normalize_pod_grafana_container_hint(value)
                if normalized:
                    return normalized

        seed_series = params.get("pod_monitor_series")
        if isinstance(seed_series, list):
            for item in seed_series:
                if not isinstance(item, dict):
                    continue
                value = item.get("pod_name")
                if isinstance(value, str):
                    normalized = self._normalize_pod_grafana_container_hint(value)
                    if normalized:
                        return normalized

        return "__no_container_hint__"

    @staticmethod
    def _normalize_pod_grafana_container_hint(raw_value: str) -> Optional[str]:
        candidate = raw_value.strip()
        if not candidate:
            return None
        normalized = re.sub(r"[^A-Za-z0-9_.-]", "", candidate)
        return normalized or None

    # === Process (运行过程) ===

    def get_process(self, run_id: int) -> dict:
        """获取运行过程信息。

        用于右上角"运行时控制"或详情顶部状态流转展示。
        可选返回扩容、执行、收尾等阶段信息。

        优先从 run.params.process 获取种子数据。
        """
        from common.schemas.run import RunProcessResponse, RunProcessStage

        run = self.repo.find_by_id(run_id)
        if not run:
            return None
        self._attach_run_display_fields([run])

        params = run.params or {}

        # 1. 优先从 params.process 获取种子数据
        seed_process = params.get("process")
        if seed_process and isinstance(seed_process, dict):
            stages = []
            for s in seed_process.get("stages", []):
                if isinstance(s, dict):
                    stages.append(
                        RunProcessStage(
                            name=s.get("name", ""),
                            status=s.get("status", ""),
                            progress=s.get("progress"),
                            started_at=self._parse_ts(s.get("started_at")),
                            ended_at=self._parse_ts(s.get("ended_at")),
                            message=s.get("message"),
                        )
                    )
            return RunProcessResponse(
                run_status=run.run_status,
                run_status_detail=run.run_status_detail,
                stages=stages,
            )

        # 2. 返回基础状态信息
        fallback_stages = self._build_fallback_process_stages(run)
        return RunProcessResponse(
            run_status=run.run_status,
            run_status_detail=run.run_status_detail,
            stages=fallback_stages,
        )

    def _build_fallback_process_stages(self, run: Run) -> list:
        from common.schemas.run import RunProcessStage

        preparing_status = (
            "completed" if run.run_status != RunStatus.PREPARING else "running"
        )
        execute_status = "pending"
        finalize_status = "pending"

        if run.run_status == RunStatus.RUNNING:
            execute_status = "running"
        elif self._is_terminal_run_status(run.run_status):
            execute_status = "completed"
            finalize_status = (
                "completed" if run.run_status == RunStatus.SUCCEEDED else "failed"
            )

        stages = [
            RunProcessStage(
                name="prepare",
                status=preparing_status,
                progress=100 if preparing_status == "completed" else 50,
                started_at=run.started_at,
                message="准备执行",
            ),
            RunProcessStage(
                name="execute",
                status=execute_status,
                progress=(
                    100
                    if execute_status == "completed"
                    else (50 if execute_status == "running" else None)
                ),
                started_at=run.started_at,
                ended_at=run.ended_at if execute_status == "completed" else None,
                message="脚本执行中" if execute_status == "running" else "脚本执行",
            ),
        ]

        if self._is_terminal_run_status(run.run_status):
            stages.append(
                RunProcessStage(
                    name="finalize",
                    status=finalize_status,
                    progress=100,
                    started_at=run.ended_at or run.started_at,
                    ended_at=run.ended_at,
                    message=(
                        "执行完成"
                        if run.run_status == RunStatus.SUCCEEDED
                        else (run.run_status_detail or "执行结束")
                    ),
                )
            )

        return stages

    def get_endpoint_trends(
        self,
        run_id: int,
        metric: Optional[str] = None,
        endpoint_name: Optional[str] = None,
        step_seconds: int = 10,
    ) -> EndpointTrendResponse:
        """获取接口级趋势数据。

        用于前端多接口多折线图展示。支持按指标类型和接口名过滤。

        数据来源优先级：
        1. K6 真实 Prometheus 接口级时序（terminal run 也优先）
        2. 从 agent 获取实时数据
        3. 从 S3 归档获取
        4. run.params.endpoint_trends 种子数据（仅兜底演示/测试）
        5. 返回空数组（不返回 500）
        """
        run = self.repo.find_by_id(run_id)
        if not run:
            return EndpointTrendResponse(step_seconds=step_seconds, items=[])
        self._sync_terminal_run_from_agent_status(run)
        self._attach_run_display_fields([run], include_live_runtime_enrichment=False)

        params = run.params or {}

        has_real_metric_context = self._has_real_metric_context(run)
        seed_trends = self._extract_endpoint_trend_seed_items(params)
        prefer_persisted_terminal_seed = bool(
            seed_trends
            and self._endpoint_trend_seed_has_durable_points(
                seed_trends,
                metric,
                endpoint_name,
            )
            and has_real_metric_context
            and self._is_terminal_run_status(run.run_status)
            and self._is_jmeter_engine(run)
        )

        if has_real_metric_context and (
            self._is_k6_engine(run) or isinstance(params.get("k6_summary"), dict)
        ):
            prom_trends = self._fetch_prometheus_k6_endpoint_trends(
                run,
                metric_filter=metric,
                endpoint_filter=endpoint_name,
                step_seconds=step_seconds,
            )
            if prom_trends and prom_trends.items:
                return prom_trends

        if seed_trends and not has_real_metric_context:
            items = self._parse_endpoint_trend_seed(seed_trends, metric, endpoint_name)
            if items:
                return EndpointTrendResponse(step_seconds=step_seconds, items=items)

        if prefer_persisted_terminal_seed:
            items = self._parse_endpoint_trend_seed(seed_trends, metric, endpoint_name)
            if items:
                return EndpointTrendResponse(step_seconds=step_seconds, items=items)

        # 2. 尝试从 agent 获取
        agent_contexts = self._get_agent_contexts(run)
        if agent_contexts:
            trends = self._fetch_agent_endpoint_trends_from_all_contexts(
                run_id,
                agent_contexts,
                metric,
                endpoint_name,
                step_seconds,
            )
            if trends and trends.items:
                return trends

            live_summary = self._fetch_agent_summary_metrics_from_all_contexts(
                run_id, agent_contexts
            )
            if live_summary and live_summary.items:
                summary_row_items = self._build_endpoint_trends_from_summary_rows(
                    run,
                    [item.model_dump() for item in live_summary.items],
                    metric_filter=metric,
                    endpoint_filter=endpoint_name,
                    step_seconds=step_seconds,
                )
                if summary_row_items:
                    return EndpointTrendResponse(
                        step_seconds=step_seconds, items=summary_row_items
                    )

        # 3. 尝试从 S3 归档获取
        metrics_s3_uri = params.get("metrics_s3")
        if metrics_s3_uri:
            try:
                trends = self._fetch_s3_endpoint_trends(
                    metrics_s3_uri, metric, endpoint_name
                )
                if trends and trends.items:
                    return trends
            except Exception as exc:
                logger.warning(
                    "get_endpoint_trends s3 failed for run %s: %s", run_id, exc
                )

        summary_row_items = self._build_endpoint_trends_from_summary_rows(
            run,
            self._extract_summary_metric_rows(params),
            metric_filter=metric,
            endpoint_filter=endpoint_name,
            step_seconds=step_seconds,
        )
        if summary_row_items:
            return EndpointTrendResponse(
                step_seconds=step_seconds, items=summary_row_items
            )

        if seed_trends:
            items = self._parse_endpoint_trend_seed(seed_trends, metric, endpoint_name)
            if items:
                return EndpointTrendResponse(step_seconds=step_seconds, items=items)

        fallback = self._build_fallback_endpoint_trends(
            run, metric, endpoint_name, step_seconds
        )
        if fallback and fallback.items:
            return fallback

        # 4. 返回空数组
        return EndpointTrendResponse(step_seconds=step_seconds, items=[])

    def _build_fallback_endpoint_trends(
        self,
        run: Run,
        metric_filter: Optional[str],
        endpoint_filter: Optional[str],
        step_seconds: int,
    ) -> Optional[EndpointTrendResponse]:
        inferred_endpoint_name = self._infer_single_grpc_iteration_endpoint_name(run)
        allowed_endpoint_filters = {"overall"}
        if inferred_endpoint_name:
            allowed_endpoint_filters.add(inferred_endpoint_name)
        if endpoint_filter and endpoint_filter not in allowed_endpoint_filters:
            return None
        summary = getattr(
            run, "overview_summary", None
        ) or self._build_run_overview_summary(run, run.params or {})
        k6_iteration_only = self._is_k6_grpc_or_iteration_run(
            run
        ) and not self._has_real_metric_context(run)
        if not self._has_real_metric_context(run):
            summary_row_items = self._build_summary_row_endpoint_trend_fallback_items(
                run,
                metric_filter=metric_filter,
                endpoint_filter=endpoint_filter,
                step_seconds=step_seconds,
            )
            if summary_row_items:
                return EndpointTrendResponse(
                    step_seconds=step_seconds,
                    items=summary_row_items,
                )
        if not any(
            value is not None
            for value in (
                run.total_requests,
                run.rps,
                run.avg_rt_ms,
                run.p95_rt_ms,
                run.p99_rt_ms,
                summary.total_requests if summary else None,
                summary.throughput if summary else None,
                self._build_k6_iteration_fallback_total_requests(run),
            )
        ):
            return None

        if k6_iteration_only:
            items = self._build_overall_endpoint_trend_fallback_items(
                run,
                metric_filter=metric_filter,
                step_seconds=step_seconds,
                existing_metrics=set(),
                endpoint_name=inferred_endpoint_name or "overall",
            )
            return (
                EndpointTrendResponse(step_seconds=step_seconds, items=items)
                if items
                else None
            )

        metric_map = {
            EndpointTrendMetric.THROUGHPUT.value: MetricName.RPS.value,
            EndpointTrendMetric.RT_AVG_MS.value: MetricName.RT_AVG_MS.value,
            EndpointTrendMetric.RT_P95_MS.value: MetricName.RT_P95_MS.value,
            EndpointTrendMetric.RT_P99_MS.value: MetricName.RT_P99_MS.value,
            EndpointTrendMetric.ERROR_RATE.value: MetricName.ERROR_RATE.value,
        }
        metrics_metric = metric_map.get(metric_filter) if metric_filter else None
        if self._has_real_metric_context(run):
            requested_metrics = (
                [metrics_metric]
                if metrics_metric
                else [
                    MetricName.RPS.value,
                    MetricName.RT_AVG_MS.value,
                    MetricName.RT_P95_MS.value,
                    MetricName.RT_P99_MS.value,
                    MetricName.ERROR_RATE.value,
                ]
            )
            metrics = self._collect_real_metrics_by_metric(
                run,
                metric_names=requested_metrics,
                step_seconds=step_seconds,
            )
        else:
            metrics = self.get_metrics(
                run.run_id, metric=metrics_metric, step_seconds=step_seconds
            )
        reverse_metric_map = {
            MetricName.RPS: EndpointTrendMetric.THROUGHPUT,
            MetricName.RT_AVG_MS: EndpointTrendMetric.RT_AVG_MS,
            MetricName.RT_P95_MS: EndpointTrendMetric.RT_P95_MS,
            MetricName.RT_P99_MS: EndpointTrendMetric.RT_P99_MS,
            MetricName.ERROR_RATE: EndpointTrendMetric.ERROR_RATE,
        }
        items: list[EndpointTrendSeries] = []
        response_step_seconds = metrics.step_seconds if metrics else step_seconds
        existing_metrics: set[EndpointTrendMetric] = set()
        if metrics:
            for series in metrics.series:
                endpoint_metric = reverse_metric_map.get(series.metric)
                if endpoint_metric is None:
                    continue
                existing_metrics.add(endpoint_metric)
                items.append(
                    EndpointTrendSeries(
                        endpoint_name=inferred_endpoint_name or "overall",
                        metric=endpoint_metric,
                        unit=series.unit,
                        points=list(series.points),
                    )
                )
        if self._has_real_metric_context(run):
            items.extend(
                self._build_overall_endpoint_trend_fallback_items(
                    run,
                    metric_filter=metric_filter,
                    step_seconds=response_step_seconds,
                    existing_metrics=existing_metrics,
                    endpoint_name=inferred_endpoint_name or "overall",
                )
            )
        return (
            EndpointTrendResponse(step_seconds=response_step_seconds, items=items)
            if items
            else None
        )

    @staticmethod
    def _extract_single_k6_grpc_endpoint_name_from_content(
        content: str,
    ) -> Optional[str]:
        if not isinstance(content, str) or not content.strip():
            return None
        matches = {
            match.strip()
            for match in _K6_GRPC_INVOKE_PATTERN.findall(content)
            if isinstance(match, str) and match.strip()
        }
        return next(iter(matches)) if len(matches) == 1 else None

    def _infer_single_grpc_iteration_endpoint_name(self, run: Run) -> Optional[str]:
        if not self._is_k6_grpc_or_iteration_run(run):
            return None

        task = self.db.query(Task).filter(Task.id == run.task_id).first()
        if not task or not task.script_id:
            return None

        script = self.db.query(Script).filter(Script.id == task.script_id).first()
        if not script or script.script_type != ScriptType.K6:
            return None

        try:
            if isinstance(script.file_path, str) and script.file_path.startswith(
                "s3://"
            ):
                bucket, key = s3_utils.parse_s3_uri(script.file_path)
                content = s3_utils.download_bytes(bucket, key).decode(
                    "utf-8", errors="ignore"
                )
            else:
                content = Path(script.file_path).read_text(
                    encoding="utf-8", errors="ignore"
                )
        except Exception:
            return None

        return self._extract_single_k6_grpc_endpoint_name_from_content(content)

    def _parse_endpoint_trend_seed(
        self,
        seed_trends: list,
        metric_filter: Optional[str],
        endpoint_filter: Optional[str],
    ) -> list[EndpointTrendSeries]:
        """解析种子数据中的接口趋势数据。"""
        items: list[EndpointTrendSeries] = []
        for trend in seed_trends:
            if not isinstance(trend, dict):
                continue

            endpoint = trend.get("endpoint_name", "")
            if endpoint_filter and endpoint != endpoint_filter:
                continue

            metric_str = trend.get("metric", "")
            if metric_filter and metric_str != metric_filter:
                continue

            try:
                metric = EndpointTrendMetric(metric_str)
            except ValueError:
                continue

            points: list[MetricPoint] = []
            for p in trend.get("points", []):
                if isinstance(p, dict):
                    ts = self._parse_ts(p.get("ts"))
                    if ts:
                        points.append(MetricPoint(ts=ts, value=p.get("value")))

            if points:
                items.append(
                    EndpointTrendSeries(
                        endpoint_name=endpoint,
                        metric=metric,
                        unit=trend.get("unit", ""),
                        points=points,
                    )
                )

        return items

    def _fetch_agent_endpoint_trends(
        self,
        agent_ctx: tuple[str, str],
        metric_filter: Optional[str],
        endpoint_filter: Optional[str],
        step_seconds: int,
    ) -> Optional[EndpointTrendResponse]:
        """从 agent 获取接口趋势数据。"""
        host, run_token = agent_ctx

        try:
            params = {"step_seconds": step_seconds}
            if metric_filter:
                params["metric"] = metric_filter
            if endpoint_filter:
                params["endpoint_name"] = endpoint_filter

            data = self._fetch_agent_json(
                host, f"/agent/runs/{run_token}/endpoint-trends", params=params
            )
            if not data:
                return None
            items: list[EndpointTrendSeries] = []
            for item in data.get("items", []):
                if not isinstance(item, dict):
                    continue
                try:
                    metric = EndpointTrendMetric(item.get("metric", ""))
                except ValueError:
                    continue

                points: list[MetricPoint] = []
                for p in item.get("points", []):
                    if isinstance(p, dict):
                        ts = self._parse_ts(p.get("ts"))
                        if ts:
                            points.append(MetricPoint(ts=ts, value=p.get("value")))

                items.append(
                    EndpointTrendSeries(
                        endpoint_name=item.get("endpoint_name", ""),
                        metric=metric,
                        unit=item.get("unit", ""),
                        points=points,
                    )
                )

            return EndpointTrendResponse(step_seconds=step_seconds, items=items)
        except Exception as exc:
            logger.warning("Failed to fetch endpoint trends from agent: %s", exc)
            return None

    def _fetch_s3_endpoint_trends(
        self,
        s3_uri: str,
        metric_filter: Optional[str],
        endpoint_filter: Optional[str],
    ) -> Optional[EndpointTrendResponse]:
        """从 S3 归档获取接口趋势数据。"""
        try:
            bucket, key = s3_utils.parse_s3_uri(s3_uri)
            data = s3_utils.download_bytes(bucket, key)
            if not data:
                return None

            import gzip

            try:
                content = gzip.decompress(data).decode("utf-8")
            except Exception:
                content = data.decode("utf-8")

            metrics_data = json.loads(content)
            endpoint_trends = metrics_data.get("endpoint_trends", [])
            if not endpoint_trends:
                return None

            items = self._parse_endpoint_trend_seed(
                endpoint_trends, metric_filter, endpoint_filter
            )
            return EndpointTrendResponse(step_seconds=10, items=items)
        except Exception as exc:
            logger.warning("Failed to fetch endpoint trends from S3: %s", exc)
            return None

    def _endpoint_trend_seed_has_durable_points(
        self,
        seed_trends: list,
        metric_filter: Optional[str],
        endpoint_filter: Optional[str],
    ) -> bool:
        matched = 0
        for trend in seed_trends:
            if not isinstance(trend, dict):
                continue
            endpoint = str(trend.get("endpoint_name") or "").strip()
            if endpoint_filter and endpoint != endpoint_filter:
                continue
            metric = str(trend.get("metric") or "").strip()
            if metric_filter and metric != metric_filter:
                continue
            try:
                EndpointTrendMetric(metric)
            except ValueError:
                continue

            points = trend.get("points")
            if not isinstance(points, list):
                return False

            valid_points = []
            for point in points:
                if not isinstance(point, dict):
                    continue
                ts = self._parse_ts(point.get("ts"))
                if not ts or ts.year < 2000:
                    return False
                valid_points.append(point)

            if len(valid_points) < 2:
                return False
            matched += 1

        return matched > 0

    def _build_endpoint_trends_from_summary_rows(
        self,
        run: Run,
        rows: list[dict[str, Any]],
        metric_filter: Optional[str],
        endpoint_filter: Optional[str],
        step_seconds: int,
    ) -> list[EndpointTrendSeries]:
        if not rows:
            return []

        start_ts = (
            self._as_utc(run.started_at)
            or self._as_utc(run.created_at)
            or datetime.now(timezone.utc).replace(microsecond=0)
        )
        end_ts = self._as_utc(run.ended_at) or (
            start_ts + timedelta(seconds=max(step_seconds, 1))
        )
        if end_ts <= start_ts:
            end_ts = start_ts + timedelta(seconds=max(step_seconds, 1))

        metric_specs = [
            (EndpointTrendMetric.THROUGHPUT, "throughput", "rps"),
            (EndpointTrendMetric.RT_AVG_MS, "avg_rt_ms", "ms"),
            (EndpointTrendMetric.RT_P95_MS, "p95_rt_ms", "ms"),
            (EndpointTrendMetric.RT_P99_MS, "p99_rt_ms", "ms"),
        ]
        items: list[EndpointTrendSeries] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            endpoint_name = str(
                row.get("endpoint_name") or row.get("name") or ""
            ).strip()
            if not endpoint_name or endpoint_name == "overall":
                continue
            if endpoint_filter and endpoint_name != endpoint_filter:
                continue
            for endpoint_metric, payload_key, unit in metric_specs:
                if metric_filter and endpoint_metric.value != metric_filter:
                    continue
                value = self._parse_seed_float(row.get(payload_key))
                if value is None:
                    continue
                items.append(
                    EndpointTrendSeries(
                        endpoint_name=endpoint_name,
                        metric=endpoint_metric,
                        unit=unit,
                        points=[
                            MetricPoint(ts=start_ts, value=float(value)),
                            MetricPoint(ts=end_ts, value=float(value)),
                        ],
                    )
                )
        return items

    def _merge_endpoint_trend_responses(
        self,
        responses: list[EndpointTrendResponse],
        step_seconds: int,
    ) -> Optional[EndpointTrendResponse]:
        merged: dict[tuple[str, EndpointTrendMetric], dict[datetime, list[float]]] = {}
        units: dict[tuple[str, EndpointTrendMetric], str] = {}
        for response in responses:
            for item in response.items:
                key = (item.endpoint_name, item.metric)
                units[key] = item.unit
                point_bucket = merged.setdefault(key, {})
                for point in item.points:
                    if point.value is None:
                        continue
                    bucketed_ts = self._bucket_endpoint_trend_ts(point.ts, step_seconds)
                    point_bucket.setdefault(bucketed_ts, []).append(float(point.value))

        if not merged:
            return None

        items: list[EndpointTrendSeries] = []
        for key in sorted(merged, key=lambda item: (item[0], item[1].value)):
            endpoint_name, metric_name = key
            points: list[MetricPoint] = []
            for ts in sorted(merged[key]):
                values = merged[key][ts]
                if not values:
                    continue
                if metric_name == EndpointTrendMetric.THROUGHPUT:
                    value = sum(values)
                else:
                    value = sum(values) / len(values)
                points.append(MetricPoint(ts=ts, value=value))
            if points:
                items.append(
                    EndpointTrendSeries(
                        endpoint_name=endpoint_name,
                        metric=metric_name,
                        unit=units.get(key, ""),
                        points=points,
                    )
                )
        return (
            EndpointTrendResponse(step_seconds=step_seconds, items=items)
            if items
            else None
        )

    def _bucket_endpoint_trend_ts(self, ts: datetime, step_seconds: int) -> datetime:
        if step_seconds <= 1:
            return ts.replace(microsecond=0)
        epoch_seconds = ts.timestamp()
        bucket_seconds = int(epoch_seconds // step_seconds) * step_seconds
        return datetime.fromtimestamp(bucket_seconds, tz=ts.tzinfo or timezone.utc)

    def _fetch_agent_endpoint_trends_from_all_contexts(
        self,
        run_id: int,
        contexts: list[tuple[str, str]],
        metric_filter: Optional[str],
        endpoint_filter: Optional[str],
        step_seconds: int,
    ) -> Optional[EndpointTrendResponse]:
        responses: list[EndpointTrendResponse] = []
        for ctx in contexts:
            try:
                trends = self._fetch_agent_endpoint_trends(
                    ctx, metric_filter, endpoint_filter, step_seconds
                )
            except Exception as exc:
                logger.warning(
                    "get_endpoint_trends agent failed for run %s host=%s: %s",
                    run_id,
                    ctx[0],
                    exc,
                )
                continue
            if trends and trends.items:
                responses.append(trends)
        return self._merge_endpoint_trend_responses(
            responses, step_seconds=step_seconds
        )
