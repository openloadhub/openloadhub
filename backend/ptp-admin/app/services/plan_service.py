from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, List, Optional, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.plan import Plan
from app.models.plan_run import PlanRun
from app.models.run import Run
from app.models.task import Task
from app.models.user import User, UserRole
from app.repositories.plan_repository import HIDDEN_PLAN_DOMAINS, PlanRepository
from app.repositories.plan_run_repository import PlanRunRepository
from app.services.plan_status_writer import (
    set_plan_status_without_touching_updated_at,
)
from app.services.report_service import ReportService
from app.services.scenario_quality_lint_service import ScenarioQualityLintService
from common.models.enums import (
    PlanExecType,
    PlanRunStatus,
    PlanStageItemType,
    PlanStatus,
    RunStatus,
)
from common.schemas.run import RunCreate, RunK6ControlRequest
from common.schemas.plan import (
    PlanRunAdvisoryFinding,
    PlanRunAdvisoryPrimaryFocus,
    PlanRunAdvisorySummary,
    PlanCreate,
    PlanExecutionEstimate,
    PlanExecuteResponse,
    PlanRunDetailResponse,
    PlanRunExecutionSummary,
    PlanRunK6BroadcastAcceptedResponse,
    PlanRunK6BroadcastItem,
    PlanRunK6BroadcastRequest,
    PlanRunK6BroadcastResponse,
    PlanRunK6BroadcastTaskStatusResponse,
    PlanRunRoundItem,
    PlanRunReportItem,
    PlanRunReportSummary,
    PlanRunSelectedInterfaceSummary,
    PlanRunSelectedRoundSummary,
    PlanRunSelectedTaskSummary,
    PlanRunSelectedRunSummary,
    PlanRunTaskExecRecord,
    PlanRunResponse,
    PlanUpdate,
)
from app.services.run_service import RunService

logger = logging.getLogger(__name__)

_ACTIVE_PLAN_RUN_STATUSES = {PlanRunStatus.PREPARING, PlanRunStatus.RUNNING}
_ACTIVE_RUN_STATUSES = {RunStatus.PREPARING, RunStatus.RUNNING}
_TERMINAL_PLAN_RUN_STATUSES = {
    PlanRunStatus.SUCCEEDED,
    PlanRunStatus.FAILED,
    PlanRunStatus.STOPPED,
}
STALE_ACTIVE_PLAN_RUN_GRACE_SECONDS = int(
    os.getenv("PLAN_RUN_STALE_ACTIVE_GRACE_SECONDS", "300")
)
PLAN_RUN_TIMEOUT_WATCHDOG_GRACE_SECONDS = int(
    os.getenv("PLAN_RUN_TIMEOUT_WATCHDOG_GRACE_SECONDS", "60")
)
_SCENARIO_DIRECT_RUNTIME_NOT_APPLIED_DETAIL = "scenario_direct_runtime_not_applied"
_POSTPROCESSOR_SLEEP_KEYS = ("sleep_time", "sleep_seconds", "seconds")
_PLAN_RUN_STATUS_MARKER_KEYS = {
    "scheduler",
    "trigger",
    "start_type",
    "execution_session_id",
    "launch_waves_total",
    "launched_runs",
    "launch_failed_wave",
    "launch_failed_reason",
    "postprocessor_round",
    "postprocessor_stage",
    "postprocessor_item",
    "postprocessor",
    "postprocessor_seconds",
    "stop_requested",
    "stop_confirmed",
    "stop_unknown",
    "k8s_kill_preview",
}
_PLAN_RUN_STATUS_MARKER_ORDER = (
    "scheduler",
    "trigger",
    "start_type",
    "execution_session_id",
    "launch_waves_total",
    "launched_runs",
    "launch_failed_wave",
    "launch_failed_reason",
    "postprocessor_round",
    "postprocessor_stage",
    "postprocessor_item",
    "postprocessor",
    "postprocessor_seconds",
    "stop_requested",
    "stop_confirmed",
    "stop_unknown",
    "k8s_kill_preview",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _compute_plan_run_duration_seconds(
    started_at: Optional[datetime], ended_at: Optional[datetime]
) -> Optional[int]:
    start_ts = _as_utc(started_at)
    end_ts = _as_utc(ended_at)
    if start_ts is None or end_ts is None:
        return None
    delta_seconds = (end_ts - start_ts).total_seconds()
    if delta_seconds <= 0:
        return 0
    return max(1, math.ceil(delta_seconds))


def _extract_plan_run_status_markers(status_detail: Optional[str]) -> dict[str, str]:
    markers: dict[str, str] = {}
    for raw_part in str(status_detail or "").split(";"):
        part = raw_part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key in _PLAN_RUN_STATUS_MARKER_KEYS and key not in markers:
            markers[key] = value
    return markers


def _merge_plan_run_status_detail(
    existing_status_detail: Optional[str], detail: Optional[str]
) -> Optional[str]:
    markers = _extract_plan_run_status_markers(existing_status_detail)
    if detail:
        for raw_part in str(detail).split(";"):
            part = raw_part.strip()
            if not part or "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key in _PLAN_RUN_STATUS_MARKER_KEYS:
                markers[key] = value

    ordered_parts: list[str] = []
    for key in _PLAN_RUN_STATUS_MARKER_ORDER:
        value = markers.get(key)
        if value:
            ordered_parts.append(f"{key}={value}")
    if detail:
        detail_parts = [
            part.strip()
            for part in str(detail).split(";")
            if part.strip()
            and part.split("=", 1)[0].strip() not in _PLAN_RUN_STATUS_MARKER_KEYS
        ]
        ordered_parts.extend(detail_parts)
    return ";".join(ordered_parts) if ordered_parts else None


def _has_executor_owned_failed_terminal(status_detail: Optional[str]) -> bool:
    detail = str(status_detail or "")
    markers = _extract_plan_run_status_markers(detail)
    return bool(
        markers.get("launch_failed_reason")
        or "partial_failure" in detail
        or "incomplete_plan_execution" in detail
        or "launch_failed" in detail
    )


def _build_execution_session_id(plan_run_id: int) -> str:
    return f"planrun-{int(plan_run_id)}"


def _mark_plan_run_dispatch_failed(plan_run_id: int, detail: str) -> None:
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        plan_run = db.query(PlanRun).filter(PlanRun.plan_run_id == plan_run_id).first()
        if not plan_run or plan_run.status != PlanRunStatus.PREPARING:
            return
        ended_at = _utcnow()
        plan_run.status = PlanRunStatus.FAILED
        plan_run.status_detail = _merge_plan_run_status_detail(
            plan_run.status_detail, "dispatch_enqueue_failed"
        )
        plan_run.ended_at = ended_at
        duration_seconds = _compute_plan_run_duration_seconds(
            plan_run.started_at, ended_at
        )
        if duration_seconds is not None:
            plan_run.duration_seconds = duration_seconds
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _dispatch_plan_run_to_worker_async(plan_run_id: int, user_id: int) -> None:
    def _runner() -> None:
        try:
            from app.tasks.plan_executor import execute_plan_task

            execute_plan_task.delay(plan_run_id, user_id)
        except Exception as exc:  # pragma: no cover - 后台容错路径
            logger.exception("Failed to dispatch plan run %s to worker", plan_run_id)
            _mark_plan_run_dispatch_failed(
                plan_run_id,
                f"dispatch_enqueue_failed: {exc}",
            )

    threading.Thread(target=_runner, daemon=True).start()


class PlanService:
    def __init__(self, db: Session):
        self.db = db
        self.plan_repo = PlanRepository(db)
        self.plan_run_repo = PlanRunRepository(db)

    @staticmethod
    def _is_k6_engine(run: Run) -> bool:
        engine_type = getattr(run, "engine_type", None)
        raw_value = getattr(engine_type, "value", engine_type)
        return str(raw_value or "").strip().lower() == "k6"

    @staticmethod
    def _should_skip_terminal_k6_enrichment(run: Run) -> bool:
        required_fields = (
            "total_requests",
            "rps",
            "avg_rt_ms",
            "p95_rt_ms",
            "p99_rt_ms",
            "error_rate",
            "success_rate",
        )
        if not all(
            isinstance(getattr(run, field, None), (int, float))
            for field in required_fields
        ):
            return False

        params = getattr(run, "params", None)
        if isinstance(params, dict):
            summary = params.get("k6_summary")
            summary_throughput = (
                summary.get("throughput") if isinstance(summary, dict) else None
            )
            if isinstance(summary_throughput, (int, float)):
                current_rps = getattr(run, "rps", None)
                if not isinstance(current_rps, (int, float)):
                    return False
                delta_ratio = abs(float(summary_throughput) - float(current_rps)) / max(
                    abs(float(summary_throughput)),
                    1.0,
                )
                if delta_ratio > 0.05:
                    return False

        return True

    @staticmethod
    def _should_skip_terminal_summary_enrichment(run: Run) -> bool:
        required_fields = (
            "total_requests",
            "rps",
            "avg_rt_ms",
            "p95_rt_ms",
            "p99_rt_ms",
            "error_rate",
            "success_rate",
        )
        return all(
            isinstance(getattr(run, field, None), (int, float))
            for field in required_fields
        )

    @staticmethod
    def _merge_summary_metrics_into_resolved(
        *,
        resolved: dict[str, Optional[float | int]],
        run_service: RunService,
        items: list[Any],
    ) -> None:
        overall = next(
            (
                item
                for item in items
                if str(getattr(item, "endpoint_name", "") or "").strip().lower()
                == "overall"
            ),
            None,
        )
        if overall is not None:
            if resolved.get("rps") is None and isinstance(
                getattr(overall, "throughput", None), (int, float)
            ):
                resolved["rps"] = float(overall.throughput)
            if isinstance(getattr(overall, "total_requests", None), int):
                resolved["total_requests"] = int(overall.total_requests)
            if isinstance(getattr(overall, "avg_rt_ms", None), (int, float)):
                resolved["avg_rt_ms"] = float(overall.avg_rt_ms)
            if isinstance(getattr(overall, "p95_rt_ms", None), (int, float)):
                resolved["p95_rt_ms"] = float(overall.p95_rt_ms)
            if isinstance(getattr(overall, "p99_rt_ms", None), (int, float)):
                resolved["p99_rt_ms"] = float(overall.p99_rt_ms)
            return

        if not items:
            return
        (
            total_requests,
            throughput,
            avg_rt_ms,
            p95_rt_ms,
            p99_rt_ms,
            _endpoint_total,
        ) = run_service._aggregate_summary_metric_rows(items)
        if resolved.get("rps") is None and isinstance(throughput, (int, float)):
            resolved["rps"] = float(throughput)
        if isinstance(total_requests, int):
            resolved["total_requests"] = total_requests
        if isinstance(avg_rt_ms, (int, float)):
            resolved["avg_rt_ms"] = float(avg_rt_ms)
        if isinstance(p95_rt_ms, (int, float)):
            resolved["p95_rt_ms"] = float(p95_rt_ms)
        if isinstance(p99_rt_ms, (int, float)):
            resolved["p99_rt_ms"] = float(p99_rt_ms)

    @staticmethod
    def _merge_checks_into_resolved(
        *,
        resolved: dict[str, Optional[float | int]],
        run_service: RunService,
        run_id: int,
    ) -> None:
        try:
            checks = run_service.get_checks(run_id)
        except Exception as exc:
            logger.debug(
                "plan_run_detail live checks unavailable for run %s: %s",
                run_id,
                exc,
            )
            return
        success_rates = [
            float(item.success_rate)
            for item in (checks.items or [])
            if isinstance(getattr(item, "success_rate", None), (int, float))
        ]
        if not success_rates:
            return
        success_rate = sum(success_rates) / len(success_rates)
        resolved["success_rate"] = round(success_rate, 6)
        resolved["error_rate"] = round(max(0.0, min(1.0, 1 - success_rate)), 6)

    def _resolve_running_summary_live_metrics(
        self,
        run: Run,
        run_service: RunService,
        cache: dict[int, dict[str, Optional[float | int]]],
    ) -> dict[str, Optional[float | int]]:
        if run.run_id in cache:
            return cache[run.run_id]

        resolved: dict[str, Optional[float | int]] = {}
        try:
            summary = run_service.get_summary_metrics(run.run_id)
            self._merge_summary_metrics_into_resolved(
                resolved=resolved,
                run_service=run_service,
                items=summary.items or [],
            )
        except Exception as exc:
            logger.debug(
                "plan_run_detail live summary unavailable for run %s: %s",
                run.run_id,
                exc,
            )
        self._merge_checks_into_resolved(
            resolved=resolved,
            run_service=run_service,
            run_id=run.run_id,
        )
        cache[run.run_id] = resolved
        return resolved

    def _resolve_terminal_summary_metrics(
        self,
        run: Run,
        run_service: RunService,
        cache: dict[int, dict[str, Optional[float | int]]],
    ) -> dict[str, Optional[float | int]]:
        return self._resolve_running_summary_live_metrics(run, run_service, cache)

    def _resolve_running_k6_live_metrics(
        self,
        run: Run,
        run_service: RunService,
        cache: dict[int, dict[str, Optional[float | int]]],
    ) -> dict[str, Optional[float | int]]:
        if run.run_id in cache:
            return cache[run.run_id]

        resolved: dict[str, Optional[float | int]] = {}
        try:
            control = run_service.get_k6_control(run.run_id)
            observed_tps = getattr(control.summary, "observed_tps", None)
            if isinstance(observed_tps, (int, float)):
                resolved["rps"] = float(observed_tps)
        except Exception as exc:
            logger.debug(
                "plan_run_detail live k6 control unavailable for run %s: %s",
                run.run_id,
                exc,
            )

        try:
            summary = run_service.get_summary_metrics(run.run_id)
            self._merge_summary_metrics_into_resolved(
                resolved=resolved,
                run_service=run_service,
                items=summary.items or [],
            )
        except Exception as exc:
            logger.debug(
                "plan_run_detail live summary unavailable for run %s: %s",
                run.run_id,
                exc,
            )

        self._merge_checks_into_resolved(
            resolved=resolved,
            run_service=run_service,
            run_id=run.run_id,
        )
        cache[run.run_id] = resolved
        return resolved

    def _resolve_terminal_k6_summary_metrics(
        self,
        run: Run,
        run_service: RunService,
        cache: dict[int, dict[str, Optional[float | int]]],
    ) -> dict[str, Optional[float | int]]:
        if run.run_id in cache:
            return cache[run.run_id]

        resolved: dict[str, Optional[float | int]] = {}
        try:
            summary = run_service.get_summary_metrics(run.run_id)
            self._merge_summary_metrics_into_resolved(
                resolved=resolved,
                run_service=run_service,
                items=summary.items or [],
            )
        except Exception as exc:
            logger.debug(
                "plan_run_detail terminal k6 summary unavailable for run %s: %s",
                run.run_id,
                exc,
            )
        params = run.params if isinstance(run.params, dict) else {}
        k6_summary = (
            params.get("k6_summary")
            if isinstance(params.get("k6_summary"), dict)
            else {}
        )
        summary_total_requests = self._coerce_int(k6_summary.get("total_requests"))
        summary_throughput = self._coerce_float(k6_summary.get("throughput"))
        if summary_total_requests is not None:
            resolved["total_requests"] = summary_total_requests
        if summary_throughput is not None:
            resolved["rps"] = summary_throughput

        try:
            checks = run_service.get_checks(run.run_id)
            success_rates = [
                float(item.success_rate)
                for item in (checks.items or [])
                if isinstance(getattr(item, "success_rate", None), (int, float))
            ]
            if success_rates:
                success_rate = sum(success_rates) / len(success_rates)
                resolved["success_rate"] = round(success_rate, 6)
                resolved["error_rate"] = round(max(0.0, min(1.0, 1 - success_rate)), 6)
        except Exception as exc:
            logger.debug(
                "plan_run_detail terminal k6 checks unavailable for run %s: %s",
                run.run_id,
                exc,
            )

        cache[run.run_id] = resolved
        return resolved

    def create_plan(self, plan_in: PlanCreate, user_id: int) -> Plan:
        plan = Plan(
            name=plan_in.name,
            description=plan_in.description,
            status=plan_in.status,
            exec_type=plan_in.exec_type,
            cron=plan_in.cron,
            scheduled_at=plan_in.scheduled_at,
            timezone=plan_in.timezone,
            enable_round=plan_in.enable_round,
            total_round=plan_in.total_round,
            business_lines=self._normalize_string_list(plan_in.business_lines),
            collaborator_ids=self._normalize_int_list(plan_in.collaborator_ids),
            stages=[stage.model_dump(mode="json") for stage in plan_in.stages],
            created_by=user_id,
        )
        created = self.plan_repo.create(plan)
        self._decorate_plan_summaries([created])
        return created

    def list_plans(
        self,
        page: int,
        page_size: int,
        plan_id: Optional[int] = None,
        name: Optional[str] = None,
        status: Optional[PlanStatus] = None,
        exec_type: Optional[PlanExecType] = None,
        created_by: Optional[int] = None,
        created_by_name: Optional[str] = None,
        participant_id: Optional[int] = None,
        business_line: Optional[str] = None,
        env: Optional[str] = None,
        engine_type: Optional[str] = None,
        round_mode: Optional[str] = None,
        time_field: Optional[str] = None,
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
    ) -> Tuple[List[Plan], int]:
        created_from, created_to = self._normalize_started_window(
            created_from, created_to
        )
        created_by_ids: Optional[list[int]] = None
        if created_by_name:
            candidate_creator_ids = self._resolve_plan_run_creator_filter_ids(
                created_by_name
            )
            if created_by is not None:
                candidate_creator_ids = (
                    {int(created_by)}
                    if candidate_creator_ids is None
                    else candidate_creator_ids & {int(created_by)}
                )
            created_by_ids = (
                sorted(candidate_creator_ids)
                if candidate_creator_ids is not None
                else None
            )
        plans, total = self.plan_repo.find_all(
            page=page,
            page_size=page_size,
            plan_id=plan_id,
            name=name,
            status=status,
            exec_type=exec_type,
            created_by=created_by if created_by_ids is None else None,
            created_by_ids=created_by_ids,
            participant_id=participant_id,
            business_line=business_line,
            env=env,
            engine_type=engine_type,
            round_mode=round_mode,
            created_from=created_from,
            created_to=created_to,
            time_field=time_field,
        )
        self._decorate_plan_summaries(plans)
        return plans, total

    def get_plan(self, plan_id: int) -> Optional[Plan]:
        plan = self.plan_repo.find_by_id(plan_id)
        if plan and getattr(plan, "domain_type", None) in HIDDEN_PLAN_DOMAINS:
            return None
        if plan:
            self._decorate_plan_summaries([plan])
        return plan

    def update_plan(
        self, plan_id: int, plan_in: PlanUpdate, user_id: Optional[int] = None
    ) -> Optional[Plan]:
        plan = self.plan_repo.find_by_id(plan_id)
        if not plan:
            return None
        self._ensure_plan_owner(plan, user_id)
        updates = plan_in.model_dump(exclude_unset=True)
        for key, value in updates.items():
            if key == "stages" and plan_in.stages is not None:
                setattr(
                    plan,
                    "stages",
                    [stage.model_dump(mode="json") for stage in plan_in.stages],
                )
                continue
            if key == "business_lines":
                setattr(plan, key, self._normalize_string_list(value))
                continue
            if key == "collaborator_ids":
                setattr(plan, key, self._normalize_int_list(value))
                continue
            setattr(plan, key, value)
        # Template edits should advance the list ordering key; runtime status paths preserve updated_at separately.
        plan.updated_at = _utcnow().replace(tzinfo=None)
        updated = self.plan_repo.update(plan)
        self._decorate_plan_summaries([updated])
        return updated

    def delete_plan(self, plan_id: int, user_id: Optional[int] = None) -> bool:
        plan = self.plan_repo.find_by_id(plan_id)
        if not plan:
            return False
        self._ensure_plan_owner(plan, user_id)
        if self.plan_has_active_run_after_stale_recovery(plan):
            raise ValueError("Cannot delete plan while it has an active plan run")
        return self.plan_repo.delete(plan_id)

    def copy_plan(self, plan_id: int, user_id: int) -> Optional[Plan]:
        source = self.plan_repo.find_by_id(plan_id)
        if not source:
            return None
        self._ensure_plan_owner(source, user_id)

        copied = Plan(
            name=f"{source.name}-copy",
            description=source.description,
            status=PlanStatus.READY,
            exec_type=source.exec_type,
            cron=source.cron,
            scheduled_at=source.scheduled_at,
            timezone=source.timezone,
            enable_round=source.enable_round,
            total_round=source.total_round,
            business_lines=self._normalize_string_list(source.business_lines),
            collaborator_ids=self._normalize_int_list(source.collaborator_ids),
            stages=source.stages if isinstance(source.stages, list) else [],
            created_by=user_id,
        )
        created = self.plan_repo.create(copied)
        self._decorate_plan_summaries([created])
        return created

    def execute_plan(
        self,
        plan_id: int,
        user_id: int,
        start_type: Optional[int] = None,
    ) -> Optional[PlanExecuteResponse]:
        plan = self.plan_repo.find_by_id(plan_id)
        if not plan:
            return None
        self._ensure_plan_owner(plan, user_id)
        initial_round = 1 if self._resolve_total_rounds(plan) > 1 else None
        initial_status_detail = (
            f"trigger=manual;start_type={start_type};preparing"
            if start_type is not None
            else "trigger=manual;preparing"
        )

        plan_run = PlanRun(
            plan_id=plan.plan_id,
            plan_name=plan.name,
            status=PlanRunStatus.PREPARING,
            status_detail=initial_status_detail,
            stages_snapshot=plan.stages if isinstance(plan.stages, list) else [],
            started_at=datetime.now(timezone.utc),
            round=initial_round,
            created_by=user_id,
        )
        plan_run = self.plan_run_repo.create(plan_run)
        plan_run.status_detail = _merge_plan_run_status_detail(
            plan_run.status_detail,
            (
                f"execution_session_id={_build_execution_session_id(plan_run.plan_run_id)}"
                ";preparing"
            ),
        )
        self.db.commit()

        stages = plan.stages if isinstance(plan.stages, list) else []
        if not stages:
            plan_run.status = PlanRunStatus.FAILED
            plan_run.status_detail = _merge_plan_run_status_detail(
                plan_run.status_detail, "no_stages"
            )
            plan_run.ended_at = _utcnow()
            duration_seconds = _compute_plan_run_duration_seconds(
                plan_run.started_at, plan_run.ended_at
            )
            if duration_seconds is not None:
                plan_run.duration_seconds = duration_seconds
            plan_run = self.plan_run_repo.update(plan_run)
            return PlanExecuteResponse(
                plan_run_id=plan_run.plan_run_id,
                status=plan_run.status,
                start_type=start_type,
            )

        # 测试模式维持同步执行；真实模式先返回 control-plane 响应，再异步派发 worker。
        if os.environ.get("TESTING", "0") == "1":
            # 测试模式：同步执行核心逻辑（无 Celery）
            from app.tasks.plan_executor import execute_plan_task

            execute_plan_task(plan_run.plan_run_id, user_id)
            self.db.refresh(plan_run)
        else:
            _dispatch_plan_run_to_worker_async(plan_run.plan_run_id, user_id)

        return PlanExecuteResponse(
            plan_run_id=plan_run.plan_run_id,
            status=plan_run.status,
            start_type=start_type,
        )

    def plan_has_active_run_after_stale_recovery(self, plan: Plan) -> bool:
        active_runs = self._find_active_plan_runs(plan.plan_id)
        if active_runs:
            self._backfill_stale_active_plan_runs(active_runs)
        active_runs = self._find_active_plan_runs(plan.plan_id)
        if active_runs:
            return True
        if plan.status == PlanStatus.RUNNING:
            set_plan_status_without_touching_updated_at(self.db, plan, PlanStatus.READY)
            self.db.commit()
        return False

    def _find_active_plan_runs(self, plan_id: int) -> list[PlanRun]:
        return (
            self.db.query(PlanRun)
            .filter(
                PlanRun.plan_id == plan_id,
                PlanRun.status.in_(list(_ACTIVE_PLAN_RUN_STATUSES)),
            )
            .all()
        )

    def list_plan_runs(
        self,
        page: int,
        page_size: int,
        plan_run_id: Optional[int] = None,
        plan_id: Optional[int] = None,
        plan_name: Optional[str] = None,
        business_line: Optional[str] = None,
        env: Optional[str] = None,
        engine_type: Optional[str] = None,
        round_mode: Optional[str] = None,
        status: Optional[PlanRunStatus] = None,
        created_by: Optional[int] = None,
        created_by_name: Optional[str] = None,
        started_from: Optional[datetime] = None,
        started_to: Optional[datetime] = None,
    ) -> Tuple[List[PlanRun], int]:
        started_from, started_to = self._normalize_started_window(
            started_from, started_to
        )
        if business_line or created_by_name or env or engine_type or round_mode:
            candidate_creator_ids = self._resolve_plan_run_creator_filter_ids(
                created_by_name
            )
            if candidate_creator_ids == set():
                return [], 0
            candidate_runs = self.plan_run_repo.find_filter_candidates(
                plan_run_id=plan_run_id,
                plan_id=plan_id,
                plan_name=plan_name,
                business_line=business_line,
                env=env,
                engine_type=engine_type,
                round_mode=round_mode,
                status=status,
                created_by=created_by,
                created_by_ids=(
                    sorted(candidate_creator_ids)
                    if candidate_creator_ids is not None
                    else None
                ),
                started_from=started_from,
                started_to=started_to,
                domain_scope="plan",
            )
            if status in _ACTIVE_PLAN_RUN_STATUSES:
                stale_changed = self._backfill_stale_active_plan_runs(candidate_runs)
                if stale_changed:
                    candidate_runs = self.plan_run_repo.find_filter_candidates(
                        plan_run_id=plan_run_id,
                        plan_id=plan_id,
                        plan_name=plan_name,
                        business_line=business_line,
                        env=env,
                        engine_type=engine_type,
                        round_mode=round_mode,
                        status=status,
                        created_by=created_by,
                        created_by_ids=(
                            sorted(candidate_creator_ids)
                            if candidate_creator_ids is not None
                            else None
                        ),
                        started_from=started_from,
                        started_to=started_to,
                        domain_scope="plan",
                    )
            matched_run_ids = self._match_filtered_plan_run_ids(
                candidate_runs,
                business_line=business_line,
                env=env,
                engine_type=engine_type,
                round_mode=round_mode,
            )
            paged_ids, total = self._paginate_items(
                items=matched_run_ids,
                page=page,
                page_size=page_size,
            )
            if not paged_ids:
                return [], total
            paged_items = self.plan_run_repo.find_by_ids(paged_ids)
            if status not in _ACTIVE_PLAN_RUN_STATUSES:
                stale_changed = self._backfill_stale_active_plan_runs(paged_items)
                if stale_changed:
                    paged_items = self.plan_run_repo.find_by_ids(paged_ids)
            self._backfill_terminal_plan_run_closure_fields(paged_items)
            self._decorate_plan_run_summaries(paged_items)
            return self._sort_plan_runs_by_ids(paged_items, paged_ids), total
        runs, total = self.plan_run_repo.find_all(
            page=page,
            page_size=page_size,
            plan_run_id=plan_run_id,
            plan_id=plan_id,
            plan_name=plan_name,
            status=status,
            created_by=created_by,
            started_from=started_from,
            started_to=started_to,
            domain_scope="plan",
        )
        stale_changed = self._backfill_stale_active_plan_runs(runs)
        if stale_changed and status in _ACTIVE_PLAN_RUN_STATUSES:
            runs, total = self.plan_run_repo.find_all(
                page=page,
                page_size=page_size,
                plan_run_id=plan_run_id,
                plan_id=plan_id,
                plan_name=plan_name,
                status=status,
                created_by=created_by,
                started_from=started_from,
                started_to=started_to,
                domain_scope="plan",
            )
        self._backfill_terminal_plan_run_closure_fields(runs)
        self._decorate_plan_run_summaries(runs)
        return runs, total

    def get_plan_run(
        self,
        plan_run_id: int,
        selected_round: Optional[int] = None,
        selected_collection_id: Optional[int] = None,
        include_live_metric_enrichment: bool = True,
        include_terminal_k6_enrichment: bool = True,
        detail_mode: str = "full",
    ) -> Optional[PlanRunDetailResponse]:
        normalized_detail_mode = str(detail_mode or "full").strip().lower()
        use_lightweight_records = normalized_detail_mode == "base"
        plan_run = self.plan_run_repo.find_by_id(plan_run_id)
        if not plan_run:
            return None
        self._backfill_stale_active_plan_runs([plan_run])
        self._backfill_terminal_plan_run_closure_fields([plan_run])

        plan = self.plan_repo.find_by_id(plan_run.plan_id)
        run_ids = plan_run.launched_run_ids or self._parse_launched_run_ids(
            plan_run.status_detail
        )
        task_count_per_round = self._count_task_items(plan_run.stages_snapshot)
        total_rounds = self._resolve_total_rounds(plan)
        if task_count_per_round > 0 and run_ids:
            inferred_rounds = (
                len(run_ids) + task_count_per_round - 1
            ) // task_count_per_round
            total_rounds = max(total_rounds, inferred_rounds)
        current_round = max(
            plan_run.round or 1,
            (
                min(total_rounds, len(run_ids) // max(task_count_per_round, 1))
                if run_ids and task_count_per_round
                else 1
            ),
        )
        if (
            plan_run.status == PlanRunStatus.SUCCEEDED
            and self._has_plan_run_completed_all_expected_runs(
                plan_run=plan_run,
                total_rounds=total_rounds,
                task_items_total=task_count_per_round,
            )
        ):
            current_round = total_rounds
        effective_round = self._resolve_selected_round(
            selected_round, current_round, total_rounds
        )
        rounds = self._build_round_items(
            total_rounds=total_rounds,
            current_round=current_round,
            final_status=plan_run.status,
        )
        round_status = next(
            (item.status for item in rounds if item.round == effective_round),
            plan_run.status,
        )
        task_exec_records = self._build_task_exec_records(
            plan_run,
            selected_round=effective_round,
            round_status=round_status,
            include_live_metric_enrichment=(
                False if use_lightweight_records else include_live_metric_enrichment
            ),
            include_terminal_k6_enrichment=(
                False if use_lightweight_records else include_terminal_k6_enrichment
            ),
        )
        task_record_total = len(task_exec_records)

        if isinstance(getattr(plan_run, "advisory_summary", None), str):
            plan_run.advisory_summary = None
        payload = PlanRunDetailResponse.model_validate(plan_run)
        payload.duration_seconds = self._resolve_duration_seconds(
            payload.duration_seconds,
            payload.started_at,
            payload.ended_at,
        )
        payload.plan_exec_type = plan.exec_type if plan else None
        estimate = self._build_plan_execution_estimate(plan) if plan else None
        payload.round = current_round
        payload.rounds = rounds
        payload.selected_round = effective_round
        payload.selected_round_status = round_status
        payload.detail_mode = "base" if use_lightweight_records else "full"
        payload.task_record_total = task_record_total
        payload.task_exec_records = task_exec_records
        resolved_collection_id = self._resolve_selected_collection_id(
            selected_collection_id, task_exec_records
        )
        payload.launched_run_ids = run_ids
        payload.round_total = max(total_rounds, 1)
        payload.round_mode_label = self._label_round_mode(total_rounds)
        payload.execution_summary = self._build_execution_summary(
            task_exec_records, run_ids
        )
        self._attach_mixed_run_peak_qps(
            execution_summary=payload.execution_summary,
            records=task_exec_records,
            plan=plan,
        )
        payload.selected_round_summary = self._build_selected_round_summary(
            selected_round=effective_round,
            rounds=rounds,
            records=task_exec_records,
            launched_run_ids=run_ids,
        )
        payload.selected_collection_id = resolved_collection_id
        payload.selected_task_summary = self._build_selected_task_summary(
            task_exec_records, resolved_collection_id
        )
        payload.selected_interface_summary = self._build_selected_interface_summary(
            task_exec_records, resolved_collection_id
        )
        payload.selected_run_summary = self._build_selected_run_summary(
            task_exec_records, resolved_collection_id
        )
        payload.report_summary = self._build_report_summary(
            task_exec_records, resolved_collection_id
        )
        payload.advisory_summary = self._build_advisory_summary(
            execution_summary=payload.execution_summary,
            selected_round_summary=payload.selected_round_summary,
            report_summary=payload.report_summary,
            task_exec_records=task_exec_records,
        )
        payload.stage_total = self._count_stages(plan_run.stages_snapshot)
        payload.task_total = (
            payload.execution_summary.task_total if payload.execution_summary else 0
        )
        payload.postprocessor_total = (
            payload.execution_summary.postprocessor_total
            if payload.execution_summary
            else 0
        )
        payload.launched_run_total = len(run_ids)
        payload.planned_total_seconds = (
            estimate.total_seconds if estimate is not None else None
        )
        payload.planned_task_seconds = (
            estimate.task_seconds if estimate is not None else None
        )
        payload.planned_interval_seconds = (
            estimate.interval_seconds if estimate is not None else None
        )
        if plan:
            overview = self._build_plan_overview(
                plan.stages,
                explicit_business_lines=getattr(plan, "business_lines", None),
                explicit_collaborator_ids=getattr(plan, "collaborator_ids", None),
            )
            payload.env_list = overview["env_list"]
            payload.business_lines = overview["business_lines"]
            payload.engine_types = overview["engine_types"]
            payload.collaborator_ids = overview["collaborator_ids"]
            payload.collaborator_names = overview["collaborator_names"]
        payload.can_stop = self._can_stop_plan_run(plan_run.status)
        self._attach_plan_run_display_fields([payload])
        return payload

    def build_plan_run_report_manifest(
        self,
        *,
        plan_run_id: int,
        selected_round: Optional[int] = None,
        selected_collection_id: Optional[int] = None,
    ) -> Optional[tuple[str, str]]:
        detail = self.get_plan_run(
            plan_run_id,
            selected_round=selected_round,
            selected_collection_id=selected_collection_id,
        )
        if detail is None:
            return None

        summary = detail.report_summary
        advisory = detail.advisory_summary
        filename = (
            f"plan-run-{detail.plan_run_id}-round-{detail.selected_round or 1}"
            "-report-manifest.txt"
        )
        lines = [
            "批次报告清单",
            f"plan_run_id={detail.plan_run_id}",
            f"plan_id={detail.plan_id}",
            f"plan_name={detail.plan_name or '-'}",
            f"selected_round={detail.selected_round or '-'}",
            f"selected_collection_id={detail.selected_collection_id or '-'}",
            f"plan_run_status={detail.status}",
            f"generated_at={_utcnow().isoformat()}",
        ]
        if advisory is not None:
            lines.extend(
                [
                    "",
                    "advisory:",
                    f"verdict={advisory.verdict}",
                    f"summary={advisory.summary_text}",
                ]
            )
            if advisory.recommended_actions:
                lines.append("recommended_actions:")
                lines.extend(f"- {item}" for item in advisory.recommended_actions)

        if summary is None:
            lines.extend(["", "report_summary=missing"])
            return filename, "\n".join(lines) + "\n"

        lines.extend(
            [
                "",
                "report_summary:",
                f"conclusion_status={summary.conclusion_status}",
                f"conclusion_text={summary.conclusion_text}",
                f"linked_run_total={summary.linked_run_total}",
                f"ready_total={summary.ready_total}",
                f"template_fallback_total={summary.template_fallback_total}",
                f"missing_total={summary.missing_total}",
                f"selected_run_status={summary.selected_run_status or '-'}",
                f"selected_run_report_id={summary.selected_run_report_id or '-'}",
                f"selected_run_report_name={summary.selected_run_report_name or '-'}",
                f"selected_run_message={summary.selected_run_message or '-'}",
                "",
                "items:",
            ]
        )
        if not summary.items:
            lines.append("- none")
        else:
            for index, item in enumerate(summary.items, start=1):
                lines.append(
                    f"{index}. collection_id={item.collection_id or '-'}"
                    f" | run_id={item.run_id}"
                    f" | task={item.task_name or '-'}"
                    f" | status={item.resolution_status}"
                    f" | report_id={item.report_id or '-'}"
                    f" | report_name={item.report_name or '-'}"
                    f" | message={item.message}"
                )
        return filename, "\n".join(lines) + "\n"

    def _decorate_plan_summaries(
        self,
        plans: list[Plan],
        *,
        overview_map: Optional[dict[int, dict[str, Any]]] = None,
        user_map: Optional[dict[int, str]] = None,
    ) -> None:
        self._attach_plan_display_fields(plans, user_map=user_map)
        resolved_overview_map = overview_map or self._build_plan_overview_map(plans)
        for plan in plans:
            overview = resolved_overview_map.get(
                int(plan.plan_id),
                self._build_plan_overview(
                    plan.stages,
                    explicit_business_lines=getattr(plan, "business_lines", None),
                    explicit_collaborator_ids=getattr(plan, "collaborator_ids", None),
                ),
            )
            plan.stage_total = overview["stage_total"]
            plan.task_total = overview["task_total"]
            plan.postprocessor_total = overview["postprocessor_total"]
            plan.env_list = overview["env_list"]
            plan.business_lines = overview["business_lines"]
            plan.engine_types = overview["engine_types"]
            plan.collaborator_ids = overview["collaborator_ids"]
            plan.collaborator_names = overview["collaborator_names"]
            plan.secondary_summary = self._build_plan_secondary_summary(plan)
            plan.execution_estimate = self._build_plan_execution_estimate(plan)

    def _decorate_plan_run_summaries(self, plan_runs: list[PlanRun]) -> None:
        self._populate_plan_run_summary_core(plan_runs)
        self._attach_plan_run_failed_run_fields(plan_runs)
        self._attach_plan_run_advisory_fields(plan_runs)
        self._attach_plan_run_display_fields(plan_runs)

    def _populate_plan_run_summary_core(self, plan_runs: list[PlanRun]) -> None:
        if not plan_runs:
            return
        plan_ids = sorted({int(item.plan_id) for item in plan_runs if item.plan_id})
        plan_map = (
            {
                plan.plan_id: plan
                for plan in self.db.query(Plan).filter(Plan.plan_id.in_(plan_ids)).all()
            }
            if plan_ids
            else {}
        )
        plan_run_overview_map = self._build_plan_run_overview_map(plan_runs, plan_map)

        for plan_run in plan_runs:
            plan = plan_map.get(plan_run.plan_id)
            plan_run.plan_exec_type = plan.exec_type if plan else None
            estimate = self._build_plan_execution_estimate(plan) if plan else None
            stages = (
                plan_run.stages_snapshot
                if isinstance(plan_run.stages_snapshot, list)
                and plan_run.stages_snapshot
                else (plan.stages if plan and isinstance(plan.stages, list) else [])
            )
            plan_run.stage_total = self._count_stages(stages)
            plan_run.task_total = self._count_task_items(stages)
            plan_run.postprocessor_total = self._count_postprocessor_items(stages)
            plan_run.launched_run_total = len(
                plan_run.launched_run_ids
                or self._parse_launched_run_ids(plan_run.status_detail)
            )
            plan_run.round_total = self._resolve_total_rounds(plan)
            plan_run.planned_total_seconds = (
                estimate.total_seconds if estimate is not None else None
            )
            plan_run.planned_task_seconds = (
                estimate.task_seconds if estimate is not None else None
            )
            plan_run.planned_interval_seconds = (
                estimate.interval_seconds if estimate is not None else None
            )
            overview = plan_run_overview_map.get(int(plan_run.plan_run_id))
            if overview is None:
                overview = self._build_plan_overview(
                    stages,
                    explicit_business_lines=(
                        getattr(plan, "business_lines", None) if plan else None
                    ),
                    explicit_collaborator_ids=(
                        getattr(plan, "collaborator_ids", None) if plan else None
                    ),
                )
            plan_run.env_list = overview["env_list"]
            plan_run.business_lines = overview["business_lines"]
            plan_run.engine_types = overview["engine_types"]
            plan_run.collaborator_names = overview["collaborator_names"]
            plan_run.can_stop = self._can_stop_plan_run(plan_run.status)
            plan_run.latest_failed_run_id = None
            plan_run.latest_failed_task_name = None

    def _attach_plan_run_failed_run_fields(self, plan_runs: list[PlanRun]) -> None:
        run_ids: set[int] = set()
        run_id_to_plan_run: dict[int, list[PlanRun]] = {}
        for plan_run in plan_runs:
            for raw_run_id in plan_run.launched_run_ids or self._parse_launched_run_ids(
                plan_run.status_detail
            ):
                try:
                    run_id = int(raw_run_id)
                except (TypeError, ValueError):
                    continue
                if run_id <= 0:
                    continue
                run_ids.add(run_id)
                run_id_to_plan_run.setdefault(run_id, []).append(plan_run)
        if not run_ids:
            return

        run_rows = self.db.query(Run).filter(Run.run_id.in_(sorted(run_ids))).all()
        for run in run_rows:
            if run.run_status != RunStatus.FAILED:
                continue
            for plan_run in run_id_to_plan_run.get(int(run.run_id), []):
                current_latest = getattr(plan_run, "latest_failed_run_id", None)
                if current_latest is not None and int(current_latest) >= int(
                    run.run_id
                ):
                    continue
                plan_run.latest_failed_run_id = int(run.run_id)
                plan_run.latest_failed_task_name = run.task_name

    def _attach_plan_run_advisory_fields(self, plan_runs: list[PlanRun]) -> None:
        if not plan_runs:
            return
        report_scan_limit = self._resolve_list_report_advisory_scan_limit()
        scannable_run_ids_by_plan_run: dict[int, list[int]] = {}
        all_scannable_run_ids: list[int] = []
        seen_scannable_run_ids: set[int] = set()
        for plan_run in plan_runs:
            run_ids = plan_run.launched_run_ids or self._parse_launched_run_ids(
                plan_run.status_detail
            )
            launched_total = len(run_ids)
            if launched_total > report_scan_limit:
                plan_run.report_ready_total = 0
                plan_run.report_template_fallback_total = 0
                plan_run.report_missing_total = 0
                plan_run.advisory_verdict = "unknown"
                plan_run.advisory_summary = (
                    f"已启动 Run {launched_total}，列表页跳过逐 Run 报告探测；"
                    "请进入详情或报告页查看完整证据。"
                )
                continue

            scannable_run_ids_by_plan_run[int(plan_run.plan_run_id)] = [
                int(run_id) for run_id in run_ids
            ]
            for run_id in run_ids:
                normalized_run_id = int(run_id)
                if normalized_run_id in seen_scannable_run_ids:
                    continue
                seen_scannable_run_ids.add(normalized_run_id)
                all_scannable_run_ids.append(normalized_run_id)

        resolved_frontdoors = (
            ReportService(self.db).resolve_download_frontdoors(all_scannable_run_ids)
            if all_scannable_run_ids
            else {}
        )
        for plan_run in plan_runs:
            run_ids = scannable_run_ids_by_plan_run.get(int(plan_run.plan_run_id))
            if run_ids is None:
                continue
            ready_total = 0
            template_fallback_total = 0
            missing_total = 0
            for run_id in run_ids:
                resolution = resolved_frontdoors.get(int(run_id), {})
                status = str(resolution.get("status") or "missing")
                if status == "ready":
                    ready_total += 1
                elif status == "template_fallback":
                    template_fallback_total += 1
                else:
                    missing_total += 1

            plan_run.report_ready_total = ready_total
            plan_run.report_template_fallback_total = template_fallback_total
            plan_run.report_missing_total = missing_total
            verdict, summary = self._build_plan_run_list_advisory(
                plan_run=plan_run,
                launched_run_total=launched_total,
                ready_total=ready_total,
                template_fallback_total=template_fallback_total,
                missing_total=missing_total,
            )
            plan_run.advisory_verdict = verdict
            plan_run.advisory_summary = summary

    @staticmethod
    def _resolve_list_report_advisory_scan_limit() -> int:
        try:
            parsed = int(
                float(os.getenv("PLAN_RUN_LIST_REPORT_ADVISORY_MAX_RUNS", "200"))
            )
        except (TypeError, ValueError):
            return 200
        return parsed if parsed > 0 else 200

    @staticmethod
    def _build_plan_run_list_advisory(
        *,
        plan_run: PlanRun,
        launched_run_total: int,
        ready_total: int,
        template_fallback_total: int,
        missing_total: int,
    ) -> tuple[str, str]:
        status = plan_run.status
        if status == PlanRunStatus.FAILED:
            return "fail", "当前批次已失败，建议优先回钻失败 Run 与报告缺口。"
        if status in {PlanRunStatus.PREPARING, PlanRunStatus.RUNNING}:
            return "warn", "当前批次仍在执行中，建议结合运行态与报告就绪度持续观察。"
        if status == PlanRunStatus.STOPPED:
            return "warn", "当前批次已停止，建议确认是否为人工止损或异常中断。"
        if launched_run_total <= 0:
            return "warn", "当前批次尚未启动真实 Run，暂时不能形成稳定批次结论。"
        if missing_total > 0:
            return "warn", f"当前批次仍有 {missing_total} 个 Run 缺少报告前门。"
        if template_fallback_total > 0:
            return (
                "warn",
                f"当前批次仍有 {template_fallback_total} 个 Run 停留在待刷新模板报告。",
            )
        if ready_total == launched_run_total:
            return (
                "pass",
                f"当前批次 {ready_total}/{launched_run_total} 个 Run 报告前门已就绪，可作为通过候选。",
            )
        return "warn", "当前批次仍需结合执行状态和报告前门做人工复核。"

    def _backfill_stale_active_plan_runs(self, plan_runs: list[PlanRun]) -> bool:
        changed = False
        recovered_plan_runs: list[PlanRun] = []
        for plan_run in plan_runs:
            if plan_run.status not in _ACTIVE_PLAN_RUN_STATUSES:
                continue
            if self._backfill_stale_active_plan_run(plan_run):
                changed = True
                recovered_plan_runs.append(plan_run)
        if changed:
            self.db.commit()
            for plan_run in recovered_plan_runs:
                self._trigger_stale_recovered_plan_run_webhooks(plan_run)
                self._auto_generate_mixed_run_report_if_needed(plan_run)
        return changed

    def _trigger_stale_recovered_plan_run_webhooks(self, plan_run: PlanRun) -> None:
        try:
            from app.services.notification_service import NotificationService

            NotificationService.trigger_plan_run_terminal_webhooks(
                self.db,
                plan_run,
                user_id=getattr(plan_run, "created_by", None),
            )
        except Exception:
            logger.exception(
                "PlanRun %s stale recovery webhook trigger failed",
                getattr(plan_run, "plan_run_id", None),
            )

    def _backfill_stale_active_plan_run(self, plan_run: PlanRun) -> bool:
        started_at = self._normalize_plan_run_datetime(
            getattr(plan_run, "started_at", None)
        )
        now = _utcnow()
        run_ids = plan_run.launched_run_ids or self._parse_launched_run_ids(
            plan_run.status_detail
        )

        if not run_ids:
            if started_at is None:
                return False
            if (now - started_at).total_seconds() < STALE_ACTIVE_PLAN_RUN_GRACE_SECONDS:
                return False
            return self._finalize_stale_active_plan_run(
                plan_run=plan_run,
                final_status=PlanRunStatus.FAILED,
                detail="stale_active_recovered:no_launched_runs",
                ended_at=now,
            )

        run_rows = (
            self.db.query(Run).filter(Run.run_id.in_(list(set(run_ids)))).all()
            if run_ids
            else []
        )
        if not run_rows:
            if started_at is None:
                return False
            if (now - started_at).total_seconds() < STALE_ACTIVE_PLAN_RUN_GRACE_SECONDS:
                return False
            return self._finalize_stale_active_plan_run(
                plan_run=plan_run,
                final_status=PlanRunStatus.FAILED,
                detail="stale_active_recovered:missing_run_rows",
                ended_at=now,
            )

        if any(run.run_status in _ACTIVE_RUN_STATUSES for run in run_rows):
            return False

        latest_terminal_at = max(
            (
                _as_utc(getattr(run, "ended_at", None))
                or _as_utc(getattr(run, "updated_at", None))
                or _as_utc(getattr(run, "started_at", None))
                or started_at
                or now
            )
            for run in run_rows
        )
        if (
            latest_terminal_at is None
            or (now - latest_terminal_at).total_seconds()
            < STALE_ACTIVE_PLAN_RUN_GRACE_SECONDS
        ):
            return False

        if any(run.run_status == RunStatus.FAILED for run in run_rows):
            return self._finalize_stale_active_plan_run(
                plan_run=plan_run,
                final_status=PlanRunStatus.FAILED,
                detail="stale_active_recovered:failed_run_terminal",
                ended_at=latest_terminal_at,
            )
        stopped_rows = [run for run in run_rows if run.run_status == RunStatus.STOPPED]
        if stopped_rows:
            alert_policy_stopped = any(
                str(getattr(run, "stop_reason", "") or "").startswith("alert_policy:")
                for run in stopped_rows
            )
            if alert_policy_stopped:
                return self._finalize_stale_active_plan_run(
                    plan_run=plan_run,
                    final_status=PlanRunStatus.FAILED,
                    detail="stale_active_recovered:alert_stopped_run_terminal",
                    ended_at=latest_terminal_at,
                )
            return self._finalize_stale_active_plan_run(
                plan_run=plan_run,
                final_status=PlanRunStatus.STOPPED,
                detail="stale_active_recovered:stopped_run_terminal",
                ended_at=latest_terminal_at,
            )

        task_items_total = self._count_task_items(plan_run.stages_snapshot)
        round_total = self._resolve_total_rounds(
            self.plan_repo.find_by_id(plan_run.plan_id)
        )
        expected_runs_total = task_items_total * max(round_total, 1)

        if expected_runs_total > 0 and len(run_ids) >= expected_runs_total:
            return self._finalize_stale_active_plan_run(
                plan_run=plan_run,
                final_status=PlanRunStatus.SUCCEEDED,
                detail="stale_active_recovered:all_runs_succeeded",
                ended_at=latest_terminal_at,
            )

        plan = self.plan_repo.find_by_id(plan_run.plan_id)
        if self._is_plan_run_waiting_for_postprocessor_sleep(
            plan_run=plan_run,
            plan=plan,
            latest_terminal_at=latest_terminal_at,
            now=now,
        ):
            return False

        return self._finalize_stale_active_plan_run(
            plan_run=plan_run,
            final_status=PlanRunStatus.FAILED,
            detail="stale_active_recovered:active_state_lost",
            ended_at=latest_terminal_at,
        )

    def _is_plan_run_waiting_for_postprocessor_sleep(
        self,
        *,
        plan_run: PlanRun,
        plan: Optional[Plan],
        latest_terminal_at: datetime,
        now: datetime,
    ) -> bool:
        markers = _extract_plan_run_status_markers(
            getattr(plan_run, "status_detail", None)
        )
        if markers.get("postprocessor") != "sleep_started":
            return False
        try:
            marker_round = int(markers.get("postprocessor_round") or 0)
            seconds = int(float(markers.get("postprocessor_seconds") or 0))
        except (TypeError, ValueError):
            return False
        if marker_round <= 0 or seconds <= 0:
            return False
        total_rounds = self._resolve_total_rounds(plan)
        if marker_round >= total_rounds:
            return False
        current_round = getattr(plan_run, "round", None)
        try:
            if current_round is not None and int(current_round) != marker_round:
                return False
        except (TypeError, ValueError):
            return False
        elapsed = (now - latest_terminal_at).total_seconds()
        return elapsed < seconds + STALE_ACTIVE_PLAN_RUN_GRACE_SECONDS

    def _finalize_stale_active_plan_run(
        self,
        *,
        plan_run: PlanRun,
        final_status: PlanRunStatus,
        detail: str,
        ended_at: datetime,
    ) -> bool:
        plan_run.status = final_status
        plan_run.status_detail = _merge_plan_run_status_detail(
            getattr(plan_run, "status_detail", None),
            detail,
        )
        plan_run.ended_at = ended_at
        duration_seconds = _compute_plan_run_duration_seconds(
            getattr(plan_run, "started_at", None),
            ended_at,
        )
        if duration_seconds is not None:
            plan_run.duration_seconds = duration_seconds
        self._recover_plan_status_if_idle(
            plan_run.plan_id, excluding_plan_run_id=plan_run.plan_run_id
        )
        return True

    def _recover_plan_status_if_idle(
        self, plan_id: Optional[int], excluding_plan_run_id: Optional[int] = None
    ) -> None:
        if not plan_id:
            return
        plan = self.plan_repo.find_by_id(plan_id)
        if not plan or plan.status != PlanStatus.RUNNING:
            return
        active_query = self.db.query(PlanRun).filter(
            PlanRun.plan_id == plan.plan_id,
            PlanRun.status.in_(list(_ACTIVE_PLAN_RUN_STATUSES)),
        )
        if excluding_plan_run_id is not None:
            active_query = active_query.filter(
                PlanRun.plan_run_id != excluding_plan_run_id
            )
        if active_query.count() == 0:
            set_plan_status_without_touching_updated_at(self.db, plan, PlanStatus.READY)

    def _backfill_terminal_plan_run_closure_fields(
        self, plan_runs: list[PlanRun]
    ) -> None:
        changed = False
        changed_plan_runs: list[PlanRun] = []
        for plan_run in plan_runs:
            if plan_run.status not in {
                PlanRunStatus.SUCCEEDED,
                PlanRunStatus.FAILED,
                PlanRunStatus.STOPPED,
            }:
                continue
            if self._backfill_plan_run_closure_fields(plan_run):
                changed = True
                changed_plan_runs.append(plan_run)
        if changed:
            self.db.commit()
            for plan_run in changed_plan_runs:
                self._auto_generate_mixed_run_report_if_needed(plan_run)

    def _auto_generate_mixed_run_report_if_needed(self, plan_run: PlanRun) -> None:
        try:
            from app.services.mixed_run_report_service import MixedRunReportService

            MixedRunReportService(self.db).auto_generate_report_if_needed(
                mixed_run_id=int(plan_run.plan_run_id),
                selected_round=getattr(plan_run, "round", None),
                user_id=getattr(plan_run, "created_by", None),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "mixed_run_report_auto_hook_failed plan_run_id=%s: %s",
                getattr(plan_run, "plan_run_id", None),
                exc,
            )
            self.db.rollback()

    def _backfill_plan_run_closure_fields(self, plan_run: PlanRun) -> bool:
        changed = False
        started_at = self._normalize_plan_run_datetime(
            getattr(plan_run, "started_at", None)
        )
        ended_at = self._normalize_plan_run_datetime(
            getattr(plan_run, "ended_at", None)
        )
        duration_seconds = getattr(plan_run, "duration_seconds", None)

        if duration_seconds is not None:
            duration_seconds = max(int(duration_seconds), 0)

        if started_at is None and ended_at is not None and duration_seconds is not None:
            started_at = ended_at - timedelta(seconds=duration_seconds)
            plan_run.started_at = started_at
            changed = True
        if ended_at is None and started_at is not None and duration_seconds is not None:
            ended_at = started_at + timedelta(seconds=duration_seconds)
            plan_run.ended_at = ended_at
            changed = True
        if (
            started_at is not None
            and ended_at is not None
            and (
                duration_seconds is None
                or (
                    duration_seconds == 0
                    and (ended_at - started_at).total_seconds() > 0
                )
            )
        ):
            duration_seconds = _compute_plan_run_duration_seconds(started_at, ended_at)
        if (
            duration_seconds is not None
            and getattr(plan_run, "duration_seconds", None) != duration_seconds
        ):
            plan_run.duration_seconds = duration_seconds
            changed = True
        if self._reconcile_terminal_plan_run_status_from_runs(plan_run):
            changed = True
        return changed

    def _reconcile_terminal_plan_run_status_from_runs(self, plan_run: PlanRun) -> bool:
        run_ids = getattr(
            plan_run, "launched_run_ids", None
        ) or self._parse_launched_run_ids(getattr(plan_run, "status_detail", None))
        if not run_ids:
            return False

        run_rows = (
            self.db.query(Run).filter(Run.run_id.in_(list(set(run_ids)))).all()
            if run_ids
            else []
        )
        if run_rows:
            run_service = RunService(self.db)
            for run in run_rows:
                run_service.sync_run_terminal_status_from_agent_if_needed(run)
        if not run_rows or any(
            run.run_status in _ACTIVE_RUN_STATUSES for run in run_rows
        ):
            return False

        resolved_status: Optional[PlanRunStatus] = None
        detail: Optional[str] = None
        stopped_rows = [run for run in run_rows if run.run_status == RunStatus.STOPPED]
        failed_rows = [run for run in run_rows if run.run_status == RunStatus.FAILED]
        if stopped_rows:
            status_detail = str(getattr(plan_run, "status_detail", "") or "")
            alert_policy_stopped = (
                any(
                    str(getattr(run, "stop_reason", "") or "").startswith(
                        "alert_policy:"
                    )
                    for run in stopped_rows
                )
                or "alert_policy:" in status_detail
            )
            if alert_policy_stopped:
                resolved_status = PlanRunStatus.FAILED
                detail = "terminal_reconciled:alert_stopped_run_terminal"
            elif failed_rows:
                resolved_status = PlanRunStatus.FAILED
                detail = "terminal_reconciled:failed_run_terminal"
            else:
                resolved_status = PlanRunStatus.STOPPED
                detail = "terminal_reconciled:stopped_run_terminal"
        elif failed_rows:
            resolved_status = PlanRunStatus.FAILED
            detail = "terminal_reconciled:failed_run_terminal"
        elif all(run.run_status == RunStatus.SUCCEEDED for run in run_rows):
            task_items_total = self._count_task_items(plan_run.stages_snapshot)
            total_rounds = self._resolve_total_rounds(
                self.plan_repo.find_by_id(plan_run.plan_id)
            )
            if not self._has_plan_run_completed_all_expected_runs(
                plan_run=plan_run,
                total_rounds=total_rounds,
                task_items_total=task_items_total,
            ):
                resolved_status = None
                detail = None
            else:
                resolved_status = PlanRunStatus.SUCCEEDED
                detail = "terminal_reconciled:all_runs_succeeded"
        else:
            return False

        if (
            plan_run.status == PlanRunStatus.FAILED
            and resolved_status == PlanRunStatus.STOPPED
            and _has_executor_owned_failed_terminal(plan_run.status_detail)
        ):
            # A stopped child is evidence for closure timing, not enough to erase
            # an executor-owned failed terminal such as launch capacity failure.
            resolved_status = PlanRunStatus.FAILED
            detail = None

        latest_terminal_at = max(
            (
                _as_utc(getattr(run, "ended_at", None))
                or _as_utc(getattr(run, "updated_at", None))
                or _as_utc(getattr(run, "started_at", None))
                or _utcnow()
            )
            for run in run_rows
        )
        duration_seconds = _compute_plan_run_duration_seconds(
            self._normalize_plan_run_datetime(getattr(plan_run, "started_at", None)),
            latest_terminal_at,
        )

        changed = False
        status_changed = False
        if resolved_status is not None and plan_run.status != resolved_status:
            plan_run.status = resolved_status
            changed = True
            status_changed = True
        current_detail = getattr(plan_run, "status_detail", None)
        should_merge_detail = (
            resolved_status is not None
            and detail
            and (
                status_changed
                or not current_detail
                or (
                    detail == "terminal_reconciled:alert_stopped_run_terminal"
                    and detail not in str(current_detail or "")
                )
            )
        )
        if should_merge_detail:
            merged_detail = _merge_plan_run_status_detail(current_detail, detail)
            if current_detail != merged_detail:
                plan_run.status_detail = merged_detail
                changed = True
        if getattr(plan_run, "ended_at", None) != latest_terminal_at:
            plan_run.ended_at = latest_terminal_at
            changed = True
        if (
            duration_seconds is not None
            and getattr(plan_run, "duration_seconds", None) != duration_seconds
        ):
            plan_run.duration_seconds = duration_seconds
            changed = True
        return changed

    def _has_plan_run_completed_all_expected_runs(
        self,
        *,
        plan_run: PlanRun,
        total_rounds: int,
        task_items_total: int,
    ) -> bool:
        if task_items_total <= 0:
            return False
        expected_runs_total = task_items_total * max(total_rounds, 1)
        run_ids = getattr(
            plan_run, "launched_run_ids", None
        ) or self._parse_launched_run_ids(getattr(plan_run, "status_detail", None))
        return len(run_ids) >= expected_runs_total

    def _normalize_plan_run_datetime(
        self, value: Optional[datetime]
    ) -> Optional[datetime]:
        return _as_utc(value)

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

    def _resolve_plan_run_creator_filter_ids(
        self, created_by_name: Optional[str]
    ) -> Optional[set[int]]:
        normalized_name = str(created_by_name or "").strip().casefold()
        if not normalized_name:
            return None
        needle = f"%{created_by_name.strip()}%"
        rows = (
            self.db.query(User)
            .filter(
                or_(
                    User.full_name.like(needle),
                    User.username.like(needle),
                )
            )
            .all()
        )
        if normalized_name == "admin":
            rows = self.db.query(User).all()
        matched_ids: set[int] = set()
        for row in rows:
            label = row.full_name or row.username or ""
            normalized_label = str(label).strip()
            if normalized_label.casefold() == "administrator":
                normalized_label = "admin"
            if normalized_name in normalized_label.casefold():
                matched_ids.add(int(row.id))

        synthetic_ids: set[int] = set()
        synthetic_ids.update(
            int(user_id)
            for user_id, in self.db.query(Plan.created_by)
            .filter(Plan.created_by.is_not(None))
            .distinct()
            .all()
            if user_id is not None
        )
        synthetic_ids.update(
            int(user_id)
            for user_id, in self.db.query(PlanRun.created_by)
            .filter(PlanRun.created_by.is_not(None))
            .distinct()
            .all()
            if user_id is not None
        )
        for user_id in synthetic_ids:
            if normalized_name in f"user#{user_id}".casefold():
                matched_ids.add(user_id)
        return matched_ids

    def _resolve_plan_run_round_mode_candidate_plan_ids(
        self, round_mode: Optional[str]
    ) -> Optional[set[int]]:
        normalized_round_mode = str(round_mode or "").strip()
        if not normalized_round_mode:
            return None
        rows = self.db.query(Plan).all()
        matched_ids: set[int] = set()
        for row in rows:
            round_total = row.total_round if getattr(row, "enable_round", False) else 1
            if self._matches_round_mode_filter(normalized_round_mode, round_total):
                matched_ids.add(int(row.plan_id))
        return matched_ids

    def _attach_plan_display_fields(
        self, plans: list[Plan], user_map: Optional[dict[int, str]] = None
    ) -> None:
        latest_plan_run_map = self._build_latest_plan_run_map(plans)
        resolved_user_map = user_map or self._build_user_display_map(
            {
                int(plan.created_by)
                for plan in plans
                if getattr(plan, "created_by", None)
            }
        )
        for plan in plans:
            status_value = (
                plan.status.value
                if hasattr(plan.status, "value")
                else str(plan.status) if plan.status is not None else None
            )
            exec_type_value = (
                plan.exec_type.value
                if hasattr(plan.exec_type, "value")
                else str(plan.exec_type) if plan.exec_type is not None else None
            )
            plan.status_label = self._label_plan_status(status_value)
            plan.exec_type_label = self._label_plan_exec_type(exec_type_value)
            plan.round_mode_label = self._label_round_mode(
                plan.total_round if getattr(plan, "enable_round", False) else 1
            )
            if plan.created_by:
                created_by_id = int(plan.created_by)
                plan.created_by_name = resolved_user_map.get(
                    created_by_id, f"user#{created_by_id}"
                )
            else:
                plan.created_by_name = None
            latest_plan_run = latest_plan_run_map.get(int(plan.plan_id))
            if latest_plan_run is not None:
                plan.latest_plan_run_id = latest_plan_run.plan_run_id
                plan.latest_plan_run_status = latest_plan_run.status
                plan.latest_plan_run_status_label = getattr(
                    latest_plan_run, "status_label", None
                ) or self._label_plan_run_status(
                    latest_plan_run.status.value
                    if hasattr(latest_plan_run.status, "value")
                    else str(latest_plan_run.status)
                )
                plan.latest_plan_run_started_at = getattr(
                    latest_plan_run, "started_at", None
                )
                plan.latest_plan_run_ended_at = getattr(
                    latest_plan_run, "ended_at", None
                )
            else:
                plan.latest_plan_run_id = None
                plan.latest_plan_run_status = None
                plan.latest_plan_run_status_label = None
                plan.latest_plan_run_started_at = None
                plan.latest_plan_run_ended_at = None
            plan.scenario_quality_lint = self._build_plan_scenario_quality_lint(plan)

    def _build_plan_scenario_quality_lint(self, plan: Plan):
        task_ids: set[int] = set()
        stages = plan.stages if isinstance(plan.stages, list) else []
        for stage in stages:
            items = stage.get("items") if isinstance(stage, dict) else None
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                task_id = item.get("task_id")
                if isinstance(task_id, int) and task_id > 0:
                    task_ids.add(task_id)
        if not task_ids:
            return ScenarioQualityLintService.summarize_plan([])
        tasks = self.db.query(Task).filter(Task.id.in_(sorted(task_ids))).all()
        return ScenarioQualityLintService.summarize_plan(tasks)

    def _build_latest_plan_run_map(self, plans: list[Plan]) -> dict[int, PlanRun]:
        plan_ids = sorted(
            {int(plan.plan_id) for plan in plans if getattr(plan, "plan_id", None)}
        )
        if not plan_ids:
            return {}

        latest_plan_run_map: dict[int, PlanRun] = {}
        latest_plan_runs = (
            self.db.query(PlanRun)
            .filter(PlanRun.plan_id.in_(plan_ids))
            .order_by(PlanRun.plan_id.asc(), PlanRun.plan_run_id.desc())
            .all()
        )
        selected_runs: list[PlanRun] = []
        for plan_run in latest_plan_runs:
            plan_id = int(plan_run.plan_id)
            if plan_id in latest_plan_run_map:
                continue
            latest_plan_run_map[plan_id] = plan_run
            selected_runs.append(plan_run)

        self._backfill_terminal_plan_run_closure_fields(selected_runs)
        self._attach_plan_run_display_fields(selected_runs)
        return latest_plan_run_map

    def _build_plan_secondary_summary(self, plan: Plan) -> str:
        return " · ".join(
            [
                f"计划ID {plan.plan_id}",
                f"{getattr(plan, 'stage_total', 0) or 0} 阶段 / {getattr(plan, 'task_total', 0) or 0} 任务 / {getattr(plan, 'postprocessor_total', 0) or 0} 后置",
                f"环境 {' / '.join(getattr(plan, 'env_list', None) or []) or '-'}",
                getattr(plan, "round_mode_label", None) or "单轮执行",
            ]
        )

    def _attach_plan_run_display_fields(
        self, plan_runs: list[PlanRun | PlanRunDetailResponse]
    ) -> None:
        user_ids = {
            int(plan_run.created_by)
            for plan_run in plan_runs
            if getattr(plan_run, "created_by", None)
        }
        user_map = self._build_user_display_map(user_ids)
        for plan_run in plan_runs:
            status_value = (
                plan_run.status.value
                if hasattr(plan_run.status, "value")
                else str(plan_run.status) if plan_run.status is not None else None
            )
            exec_type_value = (
                plan_run.plan_exec_type.value
                if hasattr(getattr(plan_run, "plan_exec_type", None), "value")
                else (
                    str(plan_run.plan_exec_type)
                    if getattr(plan_run, "plan_exec_type", None) is not None
                    else None
                )
            )
            round_value = getattr(plan_run, "round", None)
            round_total_value = getattr(plan_run, "round_total", None)
            plan_run.status_label = self._label_plan_run_status(status_value)
            plan_run.plan_exec_type_label = self._label_plan_exec_type(exec_type_value)
            plan_run.round_label = self._label_round_progress(
                round_value, round_total_value
            )
            plan_run.round_mode_label = self._label_round_mode(round_total_value)
            if plan_run.created_by:
                created_by_id = int(plan_run.created_by)
                plan_run.created_by_name = user_map.get(
                    created_by_id, f"user#{created_by_id}"
                )
            else:
                plan_run.created_by_name = None
            plan_run.summary_label = self._build_plan_run_summary_label(plan_run)

    def _build_plan_run_summary_label(
        self, plan_run: PlanRun | PlanRunDetailResponse
    ) -> str:
        business_line = (
            " / ".join(getattr(plan_run, "business_lines", None) or []) or "-"
        )
        round_label = (
            getattr(plan_run, "round_label", None)
            or self._label_round_progress(
                getattr(plan_run, "round", None), getattr(plan_run, "round_total", None)
            )
            or getattr(plan_run, "round_mode_label", None)
            or "单轮执行"
        )
        return " · ".join(
            [
                f"计划ID {plan_run.plan_id}",
                business_line,
                round_label,
                f"已启动 Run {getattr(plan_run, 'launched_run_total', 0) or 0}",
            ]
        )

    def _build_plan_overview(
        self,
        stages: Any,
        explicit_business_lines: Optional[list[str]] = None,
        explicit_collaborator_ids: Optional[list[int]] = None,
        task_lookup: Optional[dict[int, Task]] = None,
        collaborator_display_map: Optional[dict[int, str]] = None,
    ) -> dict[str, Any]:
        task_ids: list[int] = []
        task_total = 0
        postprocessor_total = 0

        if isinstance(stages, list):
            for stage in stages:
                if not isinstance(stage, dict):
                    continue
                items = stage.get("items")
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == PlanStageItemType.POSTPROCESSOR.value:
                        postprocessor_total += 1
                        continue
                    task_total += 1
                    task_id = item.get("task_id")
                    try:
                        task_id_int = int(task_id)
                    except (TypeError, ValueError):
                        continue
                    if task_id_int > 0:
                        task_ids.append(task_id_int)

        unique_task_ids = sorted(set(task_ids))
        env_list: list[str] = []
        derived_business_lines: list[str] = []
        engine_types: list[str] = []
        derived_collaborator_ids: list[int] = []
        if unique_task_ids:
            if task_lookup is not None:
                task_rows = [
                    task_lookup[task_id]
                    for task_id in unique_task_ids
                    if task_id in task_lookup
                ]
            else:
                task_rows = (
                    self.db.query(Task).filter(Task.id.in_(unique_task_ids)).all()
                )
            env_set = {
                str(task.env).strip()
                for task in task_rows
                if getattr(task, "env", None)
            }
            business_line_set = {
                str((task.properties or {}).get("business_line")).strip()
                for task in task_rows
                if isinstance(getattr(task, "properties", None), dict)
                and (task.properties or {}).get("business_line")
            }
            engine_set = {
                (
                    task.engine_type.value
                    if hasattr(task.engine_type, "value")
                    else str(task.engine_type)
                )
                for task in task_rows
                if getattr(task, "engine_type", None)
            }
            collaborator_set: set[int] = set()
            for task in task_rows:
                raw_ids = getattr(task, "collaborator_ids", None)
                if not isinstance(raw_ids, list):
                    continue
                for raw_id in raw_ids:
                    try:
                        collaborator_id = int(raw_id)
                    except (TypeError, ValueError):
                        continue
                    if collaborator_id > 0:
                        collaborator_set.add(collaborator_id)
            env_list = sorted(env_set)
            derived_business_lines = sorted(business_line_set)
            engine_types = sorted(engine_set)
            derived_collaborator_ids = sorted(collaborator_set)

        if explicit_business_lines is None:
            business_lines = derived_business_lines
        else:
            business_lines = self._normalize_string_list(explicit_business_lines) or []

        if explicit_collaborator_ids is None:
            collaborator_ids = derived_collaborator_ids
        else:
            collaborator_ids = self._normalize_int_list(explicit_collaborator_ids) or []

        collaborator_map = collaborator_display_map or self._build_user_display_map(
            set(collaborator_ids)
        )
        collaborator_names = [
            collaborator_map.get(collaborator_id, f"user#{collaborator_id}")
            for collaborator_id in collaborator_ids
        ]

        return {
            "stage_total": self._count_stages(stages),
            "task_total": task_total,
            "postprocessor_total": postprocessor_total,
            "env_list": env_list,
            "business_lines": business_lines,
            "engine_types": engine_types,
            "collaborator_ids": collaborator_ids,
            "collaborator_names": collaborator_names,
        }

    def _build_plan_overview_map(self, plans: list[Plan]) -> dict[int, dict[str, Any]]:
        if not plans:
            return {}

        task_ids: set[int] = set()
        collaborator_ids: set[int] = set()
        for plan in plans:
            for raw_id in getattr(plan, "collaborator_ids", None) or []:
                try:
                    collaborator_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if collaborator_id > 0:
                    collaborator_ids.add(collaborator_id)
            stages = plan.stages if isinstance(plan.stages, list) else []
            for stage in stages:
                if not isinstance(stage, dict):
                    continue
                items = stage.get("items")
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == PlanStageItemType.POSTPROCESSOR.value:
                        continue
                    try:
                        task_id = int(item.get("task_id"))
                    except (TypeError, ValueError):
                        continue
                    if task_id > 0:
                        task_ids.add(task_id)

        task_rows = (
            self.db.query(Task).filter(Task.id.in_(sorted(task_ids))).all()
            if task_ids
            else []
        )
        task_lookup = {int(task.id): task for task in task_rows}
        for task in task_rows:
            for raw_id in getattr(task, "collaborator_ids", None) or []:
                try:
                    collaborator_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if collaborator_id > 0:
                    collaborator_ids.add(collaborator_id)

        collaborator_display_map = self._build_user_display_map(collaborator_ids)
        overview_map: dict[int, dict[str, Any]] = {}
        for plan in plans:
            overview_map[int(plan.plan_id)] = self._build_plan_overview(
                plan.stages,
                explicit_business_lines=getattr(plan, "business_lines", None),
                explicit_collaborator_ids=getattr(plan, "collaborator_ids", None),
                task_lookup=task_lookup,
                collaborator_display_map=collaborator_display_map,
            )
        return overview_map

    def _normalize_string_list(
        self, values: Optional[list[Any]]
    ) -> Optional[list[str]]:
        if values is None:
            return None
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_value in values:
            value = str(raw_value).strip()
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return normalized

    def _normalize_int_list(self, values: Optional[list[Any]]) -> Optional[list[int]]:
        if values is None:
            return None
        normalized: list[int] = []
        seen: set[int] = set()
        for raw_value in values:
            try:
                value = int(raw_value)
            except (TypeError, ValueError):
                continue
            if value <= 0 or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return normalized

    def _filter_plan_matches(
        self,
        items: list[Plan],
        participant_id: Optional[int] = None,
        business_line: Optional[str] = None,
        created_by_name: Optional[str] = None,
        env: Optional[str] = None,
        engine_type: Optional[str] = None,
        round_mode: Optional[str] = None,
        user_map: Optional[dict[int, str]] = None,
        overview_map: Optional[dict[int, dict[str, Any]]] = None,
    ) -> list[Plan]:
        normalized_name = (
            created_by_name.strip().casefold() if created_by_name else None
        )
        normalized_env = env.strip().casefold() if env else None
        normalized_engine_type = engine_type.strip().casefold() if engine_type else None
        matched_items: list[Plan] = []
        for item in items:
            if business_line and business_line not in (
                getattr(item, "business_lines", None) or []
            ):
                continue
            if participant_id:
                collaborator_ids = {
                    int(raw_id)
                    for raw_id in (getattr(item, "collaborator_ids", None) or [])
                    if isinstance(raw_id, int)
                    or (isinstance(raw_id, str) and raw_id.isdigit())
                }
                created_by = getattr(item, "created_by", None)
                try:
                    created_by_id = int(created_by) if created_by is not None else None
                except (TypeError, ValueError):
                    created_by_id = None
                if (
                    participant_id != created_by_id
                    and participant_id not in collaborator_ids
                ):
                    continue
            if normalized_name:
                created_by = getattr(item, "created_by", None)
                try:
                    created_by_id = int(created_by) if created_by is not None else None
                except (TypeError, ValueError):
                    created_by_id = None
                display_name = (
                    (
                        (user_map or {}).get(
                            created_by_id or 0,
                            (
                                f"user#{created_by_id}"
                                if created_by_id is not None
                                else ""
                            ),
                        )
                        if created_by_id is not None
                        else ""
                    )
                    .strip()
                    .casefold()
                )
                if normalized_name not in display_name:
                    continue
            if round_mode:
                round_total = (
                    item.total_round if getattr(item, "enable_round", False) else 1
                )
                if not self._matches_round_mode_filter(round_mode, round_total):
                    continue
            if normalized_env or normalized_engine_type:
                overview = (overview_map or {}).get(int(item.plan_id), {})
                if normalized_env:
                    env_values = [
                        str(value).strip().casefold()
                        for value in (overview.get("env_list") or [])
                        if str(value).strip()
                    ]
                    if normalized_env not in env_values:
                        continue
                if normalized_engine_type:
                    engine_values = [
                        str(value).strip().casefold()
                        for value in (overview.get("engine_types") or [])
                        if str(value).strip()
                    ]
                    if normalized_engine_type not in engine_values:
                        continue
            matched_items.append(item)
        return matched_items

    def _filter_plan_run_matches(
        self,
        items: list[PlanRun],
        business_line: Optional[str] = None,
        env: Optional[str] = None,
        engine_type: Optional[str] = None,
        round_mode: Optional[str] = None,
        created_by_name: Optional[str] = None,
    ) -> list[PlanRun]:
        normalized_name = (
            created_by_name.strip().casefold() if created_by_name else None
        )
        normalized_env = env.strip().casefold() if env else None
        normalized_engine_type = engine_type.strip().casefold() if engine_type else None
        matched_items: list[PlanRun] = []
        for item in items:
            if business_line and business_line not in (
                getattr(item, "business_lines", None) or []
            ):
                continue
            if normalized_env:
                env_values = [
                    str(value).strip().casefold()
                    for value in (getattr(item, "env_list", None) or [])
                    if str(value).strip()
                ]
                if normalized_env not in env_values:
                    continue
            if normalized_engine_type:
                engine_values = [
                    str(value).strip().casefold()
                    for value in (getattr(item, "engine_types", None) or [])
                    if str(value).strip()
                ]
                if normalized_engine_type not in engine_values:
                    continue
            if round_mode:
                round_total = getattr(item, "round_total", None) or 1
                if not self._matches_round_mode_filter(round_mode, round_total):
                    continue
            if normalized_name:
                display_name = (
                    (getattr(item, "created_by_name", None) or "").strip().casefold()
                )
                if normalized_name not in display_name:
                    continue
            matched_items.append(item)
        return matched_items

    def _match_filtered_plan_run_ids(
        self,
        items: list[PlanRun],
        business_line: Optional[str] = None,
        env: Optional[str] = None,
        engine_type: Optional[str] = None,
        round_mode: Optional[str] = None,
    ) -> list[int]:
        if not items:
            return []

        expected_business_lines = self._normalize_multi_value_terms(business_line)
        expected_envs = self._normalize_multi_value_terms(env)
        expected_engine_types = self._normalize_multi_value_terms(engine_type)
        requires_task_lookup = bool(
            expected_business_lines or expected_envs or expected_engine_types
        )

        plan_ids = sorted({int(item.plan_id) for item in items if item.plan_id})
        plan_map = (
            {
                int(plan.plan_id): plan
                for plan in self.db.query(Plan).filter(Plan.plan_id.in_(plan_ids)).all()
            }
            if plan_ids
            else {}
        )

        task_lookup: dict[int, Task] = {}
        plan_run_task_ids: dict[int, set[int]] = {}
        if requires_task_lookup:
            all_task_ids: set[int] = set()
            for item in items:
                task_ids = self._extract_plan_stage_task_ids(
                    self._resolve_plan_run_filter_stages(item, plan_map)
                )
                plan_run_task_ids[int(item.plan_run_id)] = task_ids
                all_task_ids.update(task_ids)
            if all_task_ids:
                task_rows = (
                    self.db.query(Task).filter(Task.id.in_(sorted(all_task_ids))).all()
                )
                task_lookup = {int(task.id): task for task in task_rows}

        matched_ids: list[int] = []
        for item in items:
            plan = plan_map.get(int(item.plan_id)) if item.plan_id else None
            if round_mode and not self._matches_round_mode_filter(
                round_mode,
                self._resolve_total_rounds(plan),
            ):
                continue
            if requires_task_lookup:
                task_ids = plan_run_task_ids.get(int(item.plan_run_id), set())
                if expected_business_lines:
                    explicit_business_lines = (
                        getattr(plan, "business_lines", None) if plan else None
                    )
                    if explicit_business_lines is not None:
                        normalized_explicit_business_lines = {
                            str(raw_line).strip().casefold()
                            for raw_line in explicit_business_lines
                            if str(raw_line).strip()
                        }
                        if not (
                            normalized_explicit_business_lines & expected_business_lines
                        ):
                            continue
                    elif not self._matches_plan_run_task_business_line(
                        task_ids, task_lookup, expected_business_lines
                    ):
                        continue
                if expected_envs and not self._matches_plan_run_task_envs(
                    task_ids, task_lookup, expected_envs
                ):
                    continue
                if (
                    expected_engine_types
                    and not self._matches_plan_run_task_engine_types(
                        task_ids, task_lookup, expected_engine_types
                    )
                ):
                    continue
            matched_ids.append(int(item.plan_run_id))
        return matched_ids

    def _resolve_plan_run_filter_stages(
        self,
        plan_run: PlanRun,
        plan_map: dict[int, Plan],
    ) -> list[Any]:
        if isinstance(plan_run.stages_snapshot, list) and plan_run.stages_snapshot:
            return plan_run.stages_snapshot
        plan = plan_map.get(int(plan_run.plan_id)) if plan_run.plan_id else None
        return plan.stages if plan and isinstance(plan.stages, list) else []

    def _extract_plan_stage_task_ids(self, stages: list[Any]) -> set[int]:
        task_ids: set[int] = set()
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            items = stage.get("items")
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == PlanStageItemType.POSTPROCESSOR.value:
                    continue
                try:
                    task_id = int(item.get("task_id"))
                except (TypeError, ValueError):
                    continue
                if task_id > 0:
                    task_ids.add(task_id)
        return task_ids

    def _matches_plan_run_task_business_line(
        self,
        task_ids: set[int],
        task_lookup: dict[int, Task],
        expected_business_lines: set[str],
    ) -> bool:
        if not expected_business_lines:
            return True
        for task_id in task_ids:
            task = task_lookup.get(task_id)
            if task is None:
                continue
            properties = task.properties if isinstance(task.properties, dict) else {}
            business_line = (
                str(properties.get("business_line") or "").strip().casefold()
            )
            if business_line and business_line in expected_business_lines:
                return True
        return False

    def _matches_plan_run_task_envs(
        self,
        task_ids: set[int],
        task_lookup: dict[int, Task],
        expected_envs: set[str],
    ) -> bool:
        if not expected_envs:
            return True
        for task_id in task_ids:
            task = task_lookup.get(task_id)
            if task is None:
                continue
            env_value = str(getattr(task, "env", "") or "").strip().casefold()
            if env_value and env_value in expected_envs:
                return True
        return False

    def _matches_plan_run_task_engine_types(
        self,
        task_ids: set[int],
        task_lookup: dict[int, Task],
        expected_engine_types: set[str],
    ) -> bool:
        if not expected_engine_types:
            return True
        for task_id in task_ids:
            task = task_lookup.get(task_id)
            if task is None:
                continue
            engine_value = (
                str(
                    getattr(
                        getattr(task, "engine_type", None),
                        "value",
                        getattr(task, "engine_type", None),
                    )
                    or ""
                )
                .strip()
                .casefold()
            )
            if engine_value and engine_value in expected_engine_types:
                return True
        return False

    def _normalize_multi_value_terms(self, raw_value: Optional[str]) -> set[str]:
        normalized_raw = str(getattr(raw_value, "value", raw_value) or "").strip()
        if not normalized_raw:
            return set()
        return {
            item.strip().casefold()
            for item in normalized_raw.split(",")
            if item.strip()
        }

    def _matches_round_mode_filter(
        self,
        round_mode: Optional[str],
        round_total: Optional[int],
    ) -> bool:
        normalized_round_mode = str(round_mode or "").strip().casefold()
        if not normalized_round_mode:
            return True
        resolved_round_total = max(int(round_total or 1), 1)
        if normalized_round_mode in {"single", "单轮"}:
            return resolved_round_total <= 1
        if normalized_round_mode in {"multi", "轮次编排"}:
            return resolved_round_total > 1

        digits = re.search(r"\d+", normalized_round_mode)
        if digits:
            return resolved_round_total == int(digits.group())

        return normalized_round_mode in {
            f"轮次{resolved_round_total}",
            f"轮次 {resolved_round_total}",
        }

    def _build_plan_run_overview_map(
        self,
        plan_runs: list[PlanRun],
        plan_map: dict[int, Plan],
    ) -> dict[int, dict[str, Any]]:
        if not plan_runs:
            return {}

        task_ids: set[int] = set()
        collaborator_ids: set[int] = set()
        plan_run_stage_sources: dict[
            int, tuple[list[Any], Optional[list[str]], Optional[list[int]]]
        ] = {}

        for plan_run in plan_runs:
            plan = plan_map.get(plan_run.plan_id)
            stages = (
                plan_run.stages_snapshot
                if isinstance(plan_run.stages_snapshot, list)
                and plan_run.stages_snapshot
                else (plan.stages if plan and isinstance(plan.stages, list) else [])
            )
            explicit_business_lines = (
                getattr(plan, "business_lines", None) if plan else None
            )
            explicit_collaborator_ids = (
                getattr(plan, "collaborator_ids", None) if plan else None
            )
            plan_run_stage_sources[int(plan_run.plan_run_id)] = (
                stages,
                explicit_business_lines,
                explicit_collaborator_ids,
            )
            for raw_id in explicit_collaborator_ids or []:
                try:
                    collaborator_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if collaborator_id > 0:
                    collaborator_ids.add(collaborator_id)
            for stage in stages:
                if not isinstance(stage, dict):
                    continue
                items = stage.get("items")
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == PlanStageItemType.POSTPROCESSOR.value:
                        continue
                    try:
                        task_id = int(item.get("task_id"))
                    except (TypeError, ValueError):
                        continue
                    if task_id > 0:
                        task_ids.add(task_id)

        task_rows = (
            self.db.query(Task).filter(Task.id.in_(sorted(task_ids))).all()
            if task_ids
            else []
        )
        task_lookup = {int(task.id): task for task in task_rows}
        for task in task_rows:
            for raw_id in getattr(task, "collaborator_ids", None) or []:
                try:
                    collaborator_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if collaborator_id > 0:
                    collaborator_ids.add(collaborator_id)

        collaborator_display_map = self._build_user_display_map(collaborator_ids)
        overview_map: dict[int, dict[str, Any]] = {}
        for plan_run in plan_runs:
            stages, explicit_business_lines, explicit_collaborator_ids = (
                plan_run_stage_sources[int(plan_run.plan_run_id)]
            )
            overview_map[int(plan_run.plan_run_id)] = self._build_plan_overview(
                stages,
                explicit_business_lines=explicit_business_lines,
                explicit_collaborator_ids=explicit_collaborator_ids,
                task_lookup=task_lookup,
                collaborator_display_map=collaborator_display_map,
            )
        return overview_map

    def _paginate_items(
        self,
        items: list[Any],
        page: int,
        page_size: int,
    ) -> tuple[list[Any], int]:
        total = len(items)
        start = max(page - 1, 0) * page_size
        end = start + page_size
        return items[start:end], total

    def _sort_plan_runs_by_ids(
        self, plan_runs: list[PlanRun], ordered_ids: list[int]
    ) -> list[PlanRun]:
        if not plan_runs or not ordered_ids:
            return []
        plan_run_map = {
            int(plan_run.plan_run_id): plan_run
            for plan_run in plan_runs
            if getattr(plan_run, "plan_run_id", None) is not None
        }
        return [
            plan_run_map[plan_run_id]
            for plan_run_id in ordered_ids
            if plan_run_id in plan_run_map
        ]

    def stop_plan_run(
        self,
        plan_run_id: int,
        user_id: Optional[int] = None,
        *,
        detail: str = "user_stopped",
        child_run_reason: Optional[str] = None,
    ) -> Optional[PlanRun]:
        plan_run = self.plan_run_repo.find_by_id(plan_run_id)
        if not plan_run:
            return None
        plan = self.plan_repo.find_by_id(plan_run.plan_id)
        self._ensure_plan_owner(plan, user_id)
        if plan_run.status in {
            PlanRunStatus.SUCCEEDED,
            PlanRunStatus.FAILED,
            PlanRunStatus.STOPPED,
        }:
            return plan_run

        # ★ 使用 launched_run_ids 字段（优先），回退到 status_detail 解析
        run_ids = plan_run.launched_run_ids or self._parse_launched_run_ids(
            plan_run.status_detail
        )
        run_ids = self._dedupe_positive_run_ids(run_ids)
        k8s_kill_preview_total = 0
        if run_ids:
            run_service = RunService(self.db)
            for run_id in run_ids:
                try:
                    stopped_run = run_service.stop_run(
                        run_id,
                        reason=child_run_reason
                        or f"stopped_from_plan_run:{plan_run_id}",
                    )
                except Exception:
                    continue
                k8s_kill_preview_total += self._extract_k8s_kill_preview_total(
                    stopped_run
                )

        stop_requested, stop_confirmed, stop_unknown = self._resolve_stop_coverage(
            run_ids
        )
        plan_run.status = PlanRunStatus.STOPPED
        stop_detail = (
            f"{detail};stop_requested={stop_requested};"
            f"stop_confirmed={stop_confirmed};stop_unknown={stop_unknown}"
        )
        if k8s_kill_preview_total > 0:
            stop_detail = f"{stop_detail};k8s_kill_preview={k8s_kill_preview_total}"
        plan_run.status_detail = _merge_plan_run_status_detail(
            plan_run.status_detail,
            stop_detail,
        )
        end_ts = _utcnow()
        plan_run.ended_at = end_ts
        duration_seconds = _compute_plan_run_duration_seconds(
            plan_run.started_at, end_ts
        )
        if duration_seconds is not None:
            plan_run.duration_seconds = duration_seconds
        updated = self.plan_run_repo.update(plan_run)

        # ★ Plan.status 回收（仅当无其他活跃 PlanRun 时才置 READY）
        if plan and plan.status == PlanStatus.RUNNING:
            active_count = (
                self.db.query(PlanRun)
                .filter(
                    PlanRun.plan_id == plan.plan_id,
                    PlanRun.status.in_(
                        [PlanRunStatus.PREPARING, PlanRunStatus.RUNNING]
                    ),
                )
                .count()
            )
            if active_count == 0:
                set_plan_status_without_touching_updated_at(
                    self.db,
                    plan,
                    PlanStatus.READY,
                )
                self.db.commit()
        self._auto_generate_mixed_run_report_if_needed(updated)
        return updated

    @staticmethod
    def _dedupe_positive_run_ids(run_ids: list[int]) -> list[int]:
        seen: set[int] = set()
        result: list[int] = []
        for raw_run_id in run_ids:
            try:
                run_id = int(raw_run_id)
            except (TypeError, ValueError):
                continue
            if run_id <= 0 or run_id in seen:
                continue
            seen.add(run_id)
            result.append(run_id)
        return result

    @staticmethod
    def _extract_k8s_kill_preview_total(run: Optional[Run]) -> int:
        params = getattr(run, "params", None)
        if not isinstance(params, dict):
            return 0
        preview = params.get("k8s_cluster_kill_preview")
        if not isinstance(preview, dict):
            return 0
        try:
            return max(0, int(preview.get("preview_total") or 0))
        except (TypeError, ValueError):
            return 0

    def _resolve_stop_coverage(self, run_ids: list[int]) -> tuple[int, int, int]:
        requested = len(run_ids)
        if requested <= 0:
            return 0, 0, 0

        try:
            runs = self.db.query(Run).filter(Run.run_id.in_(run_ids)).all()
        except Exception:
            return requested, 0, requested

        confirmed = 0
        for run in runs or []:
            status = getattr(run, "run_status", getattr(run, "status", None))
            raw_status = getattr(status, "value", status)
            if status in {
                RunStatus.SUCCEEDED,
                RunStatus.FAILED,
                RunStatus.STOPPED,
            } or str(raw_status or "").strip().lower() in {
                "succeeded",
                "failed",
                "stopped",
            }:
                confirmed += 1
        confirmed = min(confirmed, requested)
        return requested, confirmed, max(0, requested - confirmed)

    def govern_timed_out_active_plan_runs(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> list[PlanRun]:
        current_now = _as_utc(now) or _utcnow()
        governed: list[PlanRun] = []
        active_plan_runs = (
            self.db.query(PlanRun)
            .filter(PlanRun.status.in_(list(_ACTIVE_PLAN_RUN_STATUSES)))
            .order_by(PlanRun.plan_run_id.asc())
            .all()
        )

        for plan_run in active_plan_runs:
            plan = self.plan_repo.find_by_id(plan_run.plan_id)
            timeout_seconds = self._resolve_plan_run_timeout_seconds(plan)
            started_at = self._normalize_plan_run_datetime(
                getattr(plan_run, "started_at", None)
            ) or self._normalize_plan_run_datetime(
                getattr(plan_run, "created_at", None)
            )
            if timeout_seconds is None or started_at is None:
                continue
            elapsed_seconds = (current_now - started_at).total_seconds()
            if (
                elapsed_seconds
                < timeout_seconds
                + self._resolve_plan_run_timeout_watchdog_grace_seconds()
            ):
                continue
            if self._has_active_child_run_within_timeout_deadline(
                plan_run,
                current_now,
            ):
                continue

            updated = self.stop_plan_run(
                plan_run.plan_run_id,
                user_id=None,
                detail="timeout_watchdog",
                child_run_reason=f"timeout_watchdog_from_plan_run:{plan_run.plan_run_id}",
            )
            if updated is not None:
                governed.append(updated)
        return governed

    def _prepare_plan_run_k6_broadcast(
        self,
        *,
        plan_run_id: int,
        request: PlanRunK6BroadcastRequest,
        user_id: Optional[int] = None,
    ) -> tuple[int, list[tuple[PlanRunTaskExecRecord, float]], float]:
        plan_run = self.plan_run_repo.find_by_id(plan_run_id)
        if not plan_run:
            raise ValueError("PlanRun not found")
        self._backfill_stale_active_plan_runs([plan_run])
        self._backfill_terminal_plan_run_closure_fields([plan_run])

        plan = self.plan_repo.find_by_id(plan_run.plan_id)
        run_ids = plan_run.launched_run_ids or self._parse_launched_run_ids(
            plan_run.status_detail
        )
        task_count_per_round = self._count_task_items(plan_run.stages_snapshot)
        total_rounds = self._resolve_total_rounds(plan)
        if task_count_per_round > 0 and run_ids:
            inferred_rounds = (
                len(run_ids) + task_count_per_round - 1
            ) // task_count_per_round
            total_rounds = max(total_rounds, inferred_rounds)
        current_round = max(
            plan_run.round or 1,
            (
                min(total_rounds, len(run_ids) // max(task_count_per_round, 1))
                if run_ids and task_count_per_round
                else 1
            ),
        )
        if (
            plan_run.status == PlanRunStatus.SUCCEEDED
            and self._has_plan_run_completed_all_expected_runs(
                plan_run=plan_run,
                total_rounds=total_rounds,
                task_items_total=task_count_per_round,
            )
        ):
            current_round = total_rounds
        selected_round = self._resolve_selected_round(
            request.selected_round, current_round, total_rounds
        )
        rounds = self._build_round_items(
            total_rounds=total_rounds,
            current_round=current_round,
            final_status=plan_run.status,
        )
        round_status = next(
            (item.status for item in rounds if item.round == selected_round),
            plan_run.status,
        )
        records = self._build_task_exec_records(
            plan_run,
            selected_round=selected_round,
            round_status=round_status,
            include_live_metric_enrichment=False,
            include_terminal_k6_enrichment=False,
        )
        controllable_records: list[tuple[PlanRunTaskExecRecord, float]] = []

        for record in records:
            engine_type = str(record.engine_type or "").strip().lower()
            status = str(record.status or "").strip().lower()
            if engine_type != "k6" or record.run_id is None:
                continue
            if status != RunStatus.RUNNING.value:
                continue
            base_target_tps = self._extract_batch_k6_base_target_tps(record.run_params)
            if base_target_tps is None:
                continue
            controllable_records.append((record, base_target_tps))

        if not controllable_records:
            raise RuntimeError("no_running_k6_runs_with_base_target_tps")

        base_total_tps = sum(base_tps for _record, base_tps in controllable_records)
        if request.ratio is not None:
            ratio = float(request.ratio)
        elif request.target_total_tps is not None:
            if base_total_tps <= 0:
                raise RuntimeError("invalid_base_total_tps_for_broadcast")
            ratio = float(request.target_total_tps) / max(base_total_tps, 0.0001)
        else:
            raise RuntimeError("ratio_or_target_total_tps_required")

        if ratio <= 0:
            raise RuntimeError("invalid_broadcast_ratio")

        return selected_round, controllable_records, ratio

    @staticmethod
    def _resolve_scenario_direct_false_reject_recheck_timeout_seconds() -> float:
        raw = os.getenv(
            "PTP_PLANRUN_SCENARIO_DIRECT_FALSE_REJECT_RECHECK_TIMEOUT_SECONDS", "45"
        )
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 45.0

    @staticmethod
    def _resolve_scenario_direct_false_reject_recheck_poll_seconds() -> float:
        raw = os.getenv(
            "PTP_PLANRUN_SCENARIO_DIRECT_FALSE_REJECT_RECHECK_POLL_SECONDS", "3"
        )
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 3.0

    @staticmethod
    def _resolve_scenario_direct_false_reject_success_ratio() -> float:
        raw = os.getenv("PTP_PLANRUN_SCENARIO_DIRECT_FALSE_REJECT_SUCCESS_RATIO", "0.9")
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 0.9

    @staticmethod
    def _is_scenario_direct_runtime_not_applied_detail(detail: Optional[str]) -> bool:
        return _SCENARIO_DIRECT_RUNTIME_NOT_APPLIED_DETAIL in str(detail or "")

    def _recover_scenario_direct_false_rejects(
        self,
        *,
        run_service: RunService,
        candidates: list[dict[str, Any]],
        user_id: Optional[int] = None,
    ) -> dict[int, dict[str, Any]]:
        if not candidates:
            return {}

        timeout_seconds = (
            self._resolve_scenario_direct_false_reject_recheck_timeout_seconds()
        )
        poll_seconds = self._resolve_scenario_direct_false_reject_recheck_poll_seconds()
        success_ratio = self._resolve_scenario_direct_false_reject_success_ratio()
        deadline = time.monotonic() + timeout_seconds
        pending: dict[int, dict[str, Any]] = {
            int(item["run_id"]): {
                **item,
                "best_observed_tps": None,
                "last_status": None,
                "last_message": None,
            }
            for item in candidates
        }
        recovered: dict[int, dict[str, Any]] = {}

        while pending:
            for run_id in list(pending.keys()):
                candidate = pending[run_id]
                try:
                    current = run_service.get_k6_control(run_id, user_id=user_id)
                except Exception as exc:
                    candidate["last_message"] = str(exc)
                    continue

                summary = getattr(current, "summary", None)
                if summary is None:
                    continue

                controller_status = (
                    str(getattr(summary, "controller_status", "") or "").strip().lower()
                )
                controller_message = str(
                    getattr(summary, "controller_message", "") or ""
                ).strip()
                observed_tps = getattr(summary, "observed_tps", None)

                candidate["last_status"] = controller_status or None
                candidate["last_message"] = controller_message or None
                if isinstance(observed_tps, (int, float)):
                    observed_value = float(observed_tps)
                    best_observed = candidate.get("best_observed_tps")
                    if best_observed is None or observed_value > float(best_observed):
                        candidate["best_observed_tps"] = observed_value

                target_tps = float(candidate["target_tps"])
                best_observed_tps = candidate.get("best_observed_tps")
                if controller_status == "applied":
                    recovered[run_id] = candidate
                    pending.pop(run_id, None)
                    continue
                if (
                    isinstance(best_observed_tps, (int, float))
                    and target_tps > 0
                    and float(best_observed_tps) >= target_tps * success_ratio
                ):
                    recovered[run_id] = candidate
                    pending.pop(run_id, None)

            if not pending or time.monotonic() >= deadline:
                break
            if poll_seconds > 0:
                time.sleep(poll_seconds)

        return recovered

    def broadcast_plan_run_k6_control(
        self,
        plan_run_id: int,
        request: PlanRunK6BroadcastRequest,
        user_id: Optional[int] = None,
        *,
        followup_scenario_direct_false_reject: bool = False,
    ) -> PlanRunK6BroadcastResponse:
        selected_round, controllable_records, ratio = (
            self._prepare_plan_run_k6_broadcast(
                plan_run_id=plan_run_id,
                request=request,
                user_id=user_id,
            )
        )

        run_service = RunService(self.db)
        items: list[PlanRunK6BroadcastItem] = []
        success_run_total = 0
        failed_run_total = 0
        warnings: list[str] = []
        skipped_run_total = 0
        base_total_tps = sum(base_tps for _record, base_tps in controllable_records)
        followup_candidates: list[dict[str, Any]] = []
        warning_entries: list[tuple[int, str]] = []

        for record, base_target_tps in controllable_records:
            target_tps = round(float(base_target_tps) * ratio, 4)
            try:
                run_service.update_k6_control(
                    int(record.run_id),
                    RunK6ControlRequest(target_tps=target_tps),
                    user_id=user_id,
                )
                items.append(
                    PlanRunK6BroadcastItem(
                        collection_id=record.collection_id,
                        run_id=int(record.run_id),
                        task_name=record.task_name,
                        env=record.env,
                        base_target_tps=round(float(base_target_tps), 4),
                        target_tps=target_tps,
                        status=record.status,
                        success=True,
                    )
                )
                success_run_total += 1
            except Exception as exc:
                item_index = len(items)
                detail = str(exc)
                items.append(
                    PlanRunK6BroadcastItem(
                        collection_id=record.collection_id,
                        run_id=int(record.run_id),
                        task_name=record.task_name,
                        env=record.env,
                        base_target_tps=round(float(base_target_tps), 4),
                        target_tps=target_tps,
                        status=record.status,
                        success=False,
                        detail=detail,
                    )
                )
                warning_entries.append(
                    (item_index, f"{record.task_name or record.run_id}: {detail}")
                )
                failed_run_total += 1
                if (
                    followup_scenario_direct_false_reject
                    and self._is_scenario_direct_runtime_not_applied_detail(detail)
                ):
                    followup_candidates.append(
                        {
                            "item_index": item_index,
                            "run_id": int(record.run_id),
                            "target_tps": target_tps,
                        }
                    )

        recovered_indices: set[int] = set()
        if followup_candidates:
            recovered = self._recover_scenario_direct_false_rejects(
                run_service=run_service,
                candidates=followup_candidates,
                user_id=user_id,
            )
            for candidate in followup_candidates:
                recovered_state = recovered.get(int(candidate["run_id"]))
                if recovered_state is None:
                    continue
                item_index = int(candidate["item_index"])
                items[item_index].success = True
                items[item_index].detail = None
                success_run_total += 1
                failed_run_total = max(0, failed_run_total - 1)
                recovered_indices.add(item_index)

        warnings.extend(
            warning
            for item_index, warning in warning_entries
            if item_index not in recovered_indices
        )

        return PlanRunK6BroadcastResponse(
            plan_run_id=plan_run_id,
            selected_round=selected_round,
            controllable_run_total=len(controllable_records),
            success_run_total=success_run_total,
            failed_run_total=failed_run_total,
            skipped_run_total=skipped_run_total,
            ratio=round(ratio, 6),
            base_total_tps=round(base_total_tps, 4),
            target_total_tps=round(base_total_tps * ratio, 4),
            items=items,
            warnings=warnings,
        )

    def submit_plan_run_k6_control_job(
        self,
        *,
        plan_run_id: int,
        request: PlanRunK6BroadcastRequest,
        user_id: Optional[int] = None,
    ) -> PlanRunK6BroadcastAcceptedResponse:
        from app.core.celery_app import CONTROL_QUEUE
        from app.tasks.plan_executor import execute_plan_run_k6_control_task

        selected_round, controllable_records, ratio = (
            self._prepare_plan_run_k6_broadcast(
                plan_run_id=plan_run_id,
                request=request,
                user_id=user_id,
            )
        )
        base_total_tps = sum(base_tps for _record, base_tps in controllable_records)
        request_payload = request.model_dump(exclude_none=True)
        request_payload["selected_round"] = selected_round
        request_payload["async_mode"] = False
        task = execute_plan_run_k6_control_task.apply_async(
            kwargs={
                "plan_run_id": plan_run_id,
                "request_payload": request_payload,
                "user_id": user_id,
            },
            queue=CONTROL_QUEUE,
        )
        return PlanRunK6BroadcastAcceptedResponse(
            plan_run_id=plan_run_id,
            selected_round=selected_round,
            async_task_id=str(task.id),
            ratio=round(ratio, 6),
            base_total_tps=round(base_total_tps, 4),
            target_total_tps=round(base_total_tps * ratio, 4),
        )

    def get_plan_run_k6_control_job_status(
        self,
        *,
        plan_run_id: int,
        task_id: str,
        user_id: Optional[int] = None,
    ) -> PlanRunK6BroadcastTaskStatusResponse:
        selected_round = self._resolve_plan_run_k6_control_job_selected_round(
            plan_run_id
        )
        from app.tasks.plan_executor import build_plan_run_k6_control_task_status

        return build_plan_run_k6_control_task_status(
            plan_run_id=plan_run_id,
            selected_round=selected_round,
            task_id=task_id,
        )

    def _resolve_plan_run_k6_control_job_selected_round(self, plan_run_id: int) -> int:
        plan_run = self.plan_run_repo.find_by_id(plan_run_id)
        if plan_run is None:
            raise ValueError("PlanRun not found")

        plan = self.plan_repo.find_by_id(plan_run.plan_id)
        run_ids = plan_run.launched_run_ids or self._parse_launched_run_ids(
            plan_run.status_detail
        )
        task_count_per_round = self._count_task_items(plan_run.stages_snapshot)
        total_rounds = self._resolve_total_rounds(plan)
        if task_count_per_round > 0 and run_ids:
            inferred_rounds = (
                len(run_ids) + task_count_per_round - 1
            ) // task_count_per_round
            total_rounds = max(total_rounds, inferred_rounds)
        current_round = max(
            plan_run.round or 1,
            (
                min(total_rounds, len(run_ids) // max(task_count_per_round, 1))
                if run_ids and task_count_per_round
                else 1
            ),
        )
        if (
            plan_run.status == PlanRunStatus.SUCCEEDED
            and self._has_plan_run_completed_all_expected_runs(
                plan_run=plan_run,
                total_rounds=total_rounds,
                task_items_total=task_count_per_round,
            )
        ):
            current_round = total_rounds
        return self._resolve_selected_round(None, current_round, total_rounds)

    def _split_weighted_integer_by_tps(
        self,
        total: Optional[int],
        weights: list[float],
    ) -> list[Optional[int]]:
        if total is None:
            return [None] * len(weights)
        if not weights:
            return []

        bucket_count = len(weights)
        normalized_total = int(total)
        if normalized_total < bucket_count:
            raise RuntimeError("target_total_max_vus_less_than_controllable_runs")

        positive_weights = [max(float(weight), 0.0) for weight in weights]
        weight_sum = sum(positive_weights)
        if weight_sum <= 0:
            base = normalized_total // bucket_count
            remainder = normalized_total % bucket_count
            return [
                base + (1 if index < remainder else 0) for index in range(bucket_count)
            ]

        remaining_total = normalized_total - bucket_count
        raw_allocations = [
            remaining_total * (weight / weight_sum) for weight in positive_weights
        ]
        floor_allocations = [int(math.floor(value)) for value in raw_allocations]
        remainder = remaining_total - sum(floor_allocations)
        ranked_indices = sorted(
            range(bucket_count),
            key=lambda index: (
                raw_allocations[index] - floor_allocations[index],
                positive_weights[index],
            ),
            reverse=True,
        )
        for index in ranked_indices[:remainder]:
            floor_allocations[index] += 1
        return [1 + value for value in floor_allocations]

    def _extract_plan_task_entries(
        self, plan: Plan
    ) -> List[tuple[int, Optional[dict[str, Any]]]]:
        entries: list[tuple[int, Optional[dict[str, Any]]]] = []
        stages = plan.stages if isinstance(plan.stages, list) else []

        def stage_sort_value(raw_stage: dict[str, Any]) -> int:
            try:
                return int(raw_stage.get("stage", 0))
            except (TypeError, ValueError):
                return 0

        for stage in sorted(stages, key=stage_sort_value):
            items = stage.get("items") if isinstance(stage, dict) else None
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                item = self._resolve_stage_item_for_round(item, round_number=1)
                item_type = item.get("type")
                if item_type != PlanStageItemType.TASK.value:
                    continue
                task_id = item.get("task_id")
                try:
                    task_id_int = int(task_id)
                except (TypeError, ValueError):
                    continue
                if task_id_int <= 0:
                    continue
                run_params = item.get("run_params")
                entries.append(
                    (task_id_int, run_params if isinstance(run_params, dict) else None)
                )
        return entries

    def _extract_batch_k6_base_target_tps(
        self, run_params: Optional[dict[str, Any]]
    ) -> Optional[float]:
        if not isinstance(run_params, dict):
            return None
        for key in ("target_tps", "total_tps", "fixed_tps", "TARGET_TPS"):
            raw_value = run_params.get(key)
            if raw_value is None or isinstance(raw_value, bool):
                continue
            try:
                parsed = float(raw_value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
        return None

    def _build_task_exec_records(
        self,
        plan_run: PlanRun,
        selected_round: int,
        round_status: Optional[PlanRunStatus],
        include_live_metric_enrichment: bool = True,
        include_terminal_k6_enrichment: bool = True,
    ) -> list[PlanRunTaskExecRecord]:
        stages = (
            plan_run.stages_snapshot
            if isinstance(plan_run.stages_snapshot, list)
            else []
        )
        run_ids = plan_run.launched_run_ids or self._parse_launched_run_ids(
            plan_run.status_detail
        )
        task_count_per_round = self._count_task_items(stages)
        if task_count_per_round > 0:
            start_idx = max(0, selected_round - 1) * task_count_per_round
            run_ids = run_ids[start_idx : start_idx + task_count_per_round]
        else:
            run_ids = []
        run_rows = (
            self.db.query(Run).filter(Run.run_id.in_(run_ids)).all() if run_ids else []
        )
        run_map = {run.run_id: run for run in run_rows}
        run_service = RunService(self.db) if run_rows else None
        live_k6_metrics_by_run: dict[int, dict[str, Optional[float | int]]] = {}
        live_summary_metrics_by_run: dict[int, dict[str, Optional[float | int]]] = {}
        terminal_k6_metrics_by_run: dict[int, dict[str, Optional[float | int]]] = {}
        terminal_summary_metrics_by_run: dict[int, dict[str, Optional[float | int]]] = (
            {}
        )
        task_run_ids = [run_id for run_id in run_ids if run_id in run_map]
        task_cursor = 0
        collection_id = 0
        records: list[PlanRunTaskExecRecord] = []
        status_markers = _extract_plan_run_status_markers(
            getattr(plan_run, "status_detail", None)
        )

        def stage_sort_value(raw_stage: dict[str, Any]) -> int:
            try:
                return int(raw_stage.get("stage", 0))
            except (TypeError, ValueError):
                return 0

        for stage in sorted(
            [item for item in stages if isinstance(item, dict)],
            key=stage_sort_value,
        ):
            items = stage.get("items")
            if not isinstance(items, list):
                continue
            stage_number = stage_sort_value(stage)
            for item in items:
                if not isinstance(item, dict):
                    continue
                item = self._resolve_stage_item_for_round(
                    item, round_number=selected_round
                )
                collection_id += 1
                logic_type = str(item.get("type") or PlanStageItemType.TASK.value)
                assigned_run: Optional[Run] = None
                if logic_type == PlanStageItemType.TASK.value and task_cursor < len(
                    task_run_ids
                ):
                    assigned_run = run_map.get(task_run_ids[task_cursor])
                    task_cursor += 1
                assigned_run_params = (
                    assigned_run.params
                    if assigned_run and isinstance(assigned_run.params, dict)
                    else {}
                )
                live_metrics: dict[str, Optional[float | int]] = {}
                if (
                    assigned_run is not None
                    and assigned_run.run_status == RunStatus.RUNNING
                    and run_service is not None
                    and include_live_metric_enrichment
                ):
                    live_metrics = (
                        self._resolve_running_k6_live_metrics(
                            assigned_run,
                            run_service,
                            live_k6_metrics_by_run,
                        )
                        if self._is_k6_engine(assigned_run)
                        else self._resolve_running_summary_live_metrics(
                            assigned_run,
                            run_service,
                            live_summary_metrics_by_run,
                        )
                    )
                terminal_k6_metrics: dict[str, Optional[float | int]] = {}
                if (
                    assigned_run is not None
                    and assigned_run.run_status != RunStatus.RUNNING
                    and self._is_k6_engine(assigned_run)
                    and run_service is not None
                    and include_terminal_k6_enrichment
                    and not self._should_skip_terminal_k6_enrichment(assigned_run)
                ):
                    terminal_k6_metrics = self._resolve_terminal_k6_summary_metrics(
                        assigned_run,
                        run_service,
                        terminal_k6_metrics_by_run,
                    )
                terminal_summary_metrics: dict[str, Optional[float | int]] = {}
                if (
                    assigned_run is not None
                    and assigned_run.run_status != RunStatus.RUNNING
                    and not self._is_k6_engine(assigned_run)
                    and run_service is not None
                    and not self._should_skip_terminal_summary_enrichment(assigned_run)
                ):
                    terminal_summary_metrics = self._resolve_terminal_summary_metrics(
                        assigned_run,
                        run_service,
                        terminal_summary_metrics_by_run,
                    )
                assigned_summary_items = (
                    self._extract_summary_metric_items(assigned_run_params)
                    if assigned_run
                    else []
                )
                assigned_total_requests = (
                    live_metrics.get("total_requests")
                    if live_metrics.get("total_requests") is not None
                    else (
                        terminal_k6_metrics.get("total_requests")
                        if terminal_k6_metrics.get("total_requests") is not None
                        else (
                            terminal_summary_metrics.get("total_requests")
                            if terminal_summary_metrics.get("total_requests")
                            is not None
                            else (assigned_run.total_requests if assigned_run else None)
                        )
                    )
                )
                assigned_rps = (
                    live_metrics.get("rps")
                    if live_metrics.get("rps") is not None
                    else (
                        terminal_k6_metrics.get("rps")
                        if terminal_k6_metrics.get("rps") is not None
                        else (
                            terminal_summary_metrics.get("rps")
                            if terminal_summary_metrics.get("rps") is not None
                            else (assigned_run.rps if assigned_run else None)
                        )
                    )
                )
                assigned_avg_rt_ms = (
                    live_metrics.get("avg_rt_ms")
                    if live_metrics.get("avg_rt_ms") is not None
                    else (
                        terminal_k6_metrics.get("avg_rt_ms")
                        if terminal_k6_metrics.get("avg_rt_ms") is not None
                        else (
                            terminal_summary_metrics.get("avg_rt_ms")
                            if terminal_summary_metrics.get("avg_rt_ms") is not None
                            else (assigned_run.avg_rt_ms if assigned_run else None)
                        )
                    )
                )
                assigned_p95_rt_ms = (
                    live_metrics.get("p95_rt_ms")
                    if live_metrics.get("p95_rt_ms") is not None
                    else (
                        terminal_k6_metrics.get("p95_rt_ms")
                        if terminal_k6_metrics.get("p95_rt_ms") is not None
                        else (
                            terminal_summary_metrics.get("p95_rt_ms")
                            if terminal_summary_metrics.get("p95_rt_ms") is not None
                            else (assigned_run.p95_rt_ms if assigned_run else None)
                        )
                    )
                )
                assigned_p99_rt_ms = (
                    live_metrics.get("p99_rt_ms")
                    if live_metrics.get("p99_rt_ms") is not None
                    else (
                        terminal_k6_metrics.get("p99_rt_ms")
                        if terminal_k6_metrics.get("p99_rt_ms") is not None
                        else (
                            terminal_summary_metrics.get("p99_rt_ms")
                            if terminal_summary_metrics.get("p99_rt_ms") is not None
                            else (assigned_run.p99_rt_ms if assigned_run else None)
                        )
                    )
                )
                assigned_error_rate = (
                    live_metrics.get("error_rate")
                    if live_metrics.get("error_rate") is not None
                    else (
                        terminal_k6_metrics.get("error_rate")
                        if terminal_k6_metrics.get("error_rate") is not None
                        else (
                            terminal_summary_metrics.get("error_rate")
                            if terminal_summary_metrics.get("error_rate") is not None
                            else (assigned_run.error_rate if assigned_run else None)
                        )
                    )
                )
                assigned_success_rate = (
                    live_metrics.get("success_rate")
                    if live_metrics.get("success_rate") is not None
                    else (
                        terminal_k6_metrics.get("success_rate")
                        if terminal_k6_metrics.get("success_rate") is not None
                        else (
                            terminal_summary_metrics.get("success_rate")
                            if terminal_summary_metrics.get("success_rate") is not None
                            else (assigned_run.success_rate if assigned_run else None)
                        )
                    )
                )

                run_params = item.get("run_params")
                is_postprocessor = logic_type == PlanStageItemType.POSTPROCESSOR.value
                postprocessor_kind = None
                postprocessor_seconds = None
                postprocessor_status = None
                postprocessor_status_label = None
                postprocessor_status_reason = None
                postprocessor_metric_summary = None
                task_status_reason = None
                if is_postprocessor:
                    run_params = item.get("post_params")
                    postprocessor_kind, postprocessor_seconds = (
                        self._resolve_postprocessor_descriptor(run_params)
                    )
                    postprocessor_status, postprocessor_status_label = (
                        self._infer_postprocessor_record_status(
                            round_status=round_status,
                            selected_round=selected_round,
                            collection_id=collection_id,
                            status_markers=status_markers,
                        )
                    )
                    postprocessor_marker_matches = (
                        self._coerce_int(status_markers.get("postprocessor_round"))
                        == selected_round
                        and self._coerce_int(status_markers.get("postprocessor_item"))
                        == collection_id
                    )
                    postprocessor_status_reason = (
                        self._build_postprocessor_status_reason(
                            status=postprocessor_status,
                            seconds=postprocessor_seconds,
                            status_markers=status_markers,
                            marker_matches=postprocessor_marker_matches,
                            plan_run_status_detail=getattr(
                                plan_run, "status_detail", None
                            ),
                        )
                    )
                    postprocessor_metric_summary = (
                        self._build_postprocessor_metric_summary(
                            status=postprocessor_status,
                            seconds=postprocessor_seconds,
                            reason=postprocessor_status_reason,
                        )
                    )
                elif assigned_run is None:
                    inferred_status = self._infer_record_status(
                        round_status, logic_type
                    )
                    task_status_reason = self._build_unbound_task_status_reason(
                        status=inferred_status,
                        status_markers=status_markers,
                        plan_run_status_detail=getattr(plan_run, "status_detail", None),
                    )

                task_name = item.get("task_name")
                if not isinstance(task_name, str) or not task_name:
                    if logic_type == PlanStageItemType.POSTPROCESSOR.value:
                        task_name = "后置处理"
                    elif assigned_run and assigned_run.task_name:
                        task_name = assigned_run.task_name
                    elif item.get("task_id"):
                        task_name = f"Task #{item.get('task_id')}"
                    else:
                        task_name = None

                records.append(
                    PlanRunTaskExecRecord(
                        collection_id=collection_id,
                        stage=stage_number,
                        logic_type=logic_type,
                        logic_type_label=self._label_task_logic_type(logic_type),
                        task_id=(
                            int(item["task_id"])
                            if item.get("task_id") is not None
                            else None
                        ),
                        task_name=task_name,
                        run_id=assigned_run.run_id if assigned_run else None,
                        status=(
                            assigned_run.run_status.value
                            if assigned_run
                            else (
                                postprocessor_status
                                if is_postprocessor
                                else self._infer_record_status(round_status, logic_type)
                            )
                        ),
                        status_label=(
                            self._label_plan_run_status(assigned_run.run_status.value)
                            if assigned_run
                            else (
                                postprocessor_status_label
                                if is_postprocessor
                                else self._label_plan_run_status(
                                    self._infer_record_status(round_status, logic_type)
                                )
                            )
                        ),
                        started_at=assigned_run.started_at if assigned_run else None,
                        ended_at=assigned_run.ended_at if assigned_run else None,
                        run_params=run_params if isinstance(run_params, dict) else None,
                        env=assigned_run.env if assigned_run else None,
                        engine_type=(
                            assigned_run.engine_type.value
                            if assigned_run
                            and hasattr(assigned_run.engine_type, "value")
                            else (
                                str(assigned_run.engine_type)
                                if assigned_run
                                and getattr(assigned_run, "engine_type", None)
                                is not None
                                else None
                            )
                        ),
                        total_requests=(assigned_total_requests),
                        rps=(
                            float(assigned_rps)
                            if isinstance(assigned_rps, (int, float))
                            else None
                        ),
                        error_rate=(
                            float(assigned_error_rate)
                            if isinstance(assigned_error_rate, (int, float))
                            else None
                        ),
                        avg_rt_ms=(
                            float(assigned_avg_rt_ms)
                            if isinstance(assigned_avg_rt_ms, (int, float))
                            else None
                        ),
                        p95_rt_ms=(
                            float(assigned_p95_rt_ms)
                            if isinstance(assigned_p95_rt_ms, (int, float))
                            else self._extract_max_summary_metric_value(
                                assigned_summary_items, "p95_rt_ms"
                            )
                        ),
                        p99_rt_ms=(
                            float(assigned_p99_rt_ms)
                            if isinstance(assigned_p99_rt_ms, (int, float))
                            else self._extract_max_summary_metric_value(
                                assigned_summary_items, "p99_rt_ms"
                            )
                        ),
                        success_rate=(
                            float(assigned_success_rate)
                            if isinstance(assigned_success_rate, (int, float))
                            else None
                        ),
                        metric_summary=(
                            postprocessor_metric_summary
                            if is_postprocessor
                            else self._build_metric_summary(
                                duration_seconds=self._resolve_duration_seconds(
                                    (
                                        assigned_run.duration_seconds
                                        if assigned_run
                                        else None
                                    ),
                                    assigned_run.started_at if assigned_run else None,
                                    assigned_run.ended_at if assigned_run else None,
                                ),
                                total_requests=assigned_total_requests,
                                rps=(
                                    float(assigned_rps)
                                    if isinstance(assigned_rps, (int, float))
                                    else None
                                ),
                                avg_rt_ms=(
                                    float(assigned_avg_rt_ms)
                                    if isinstance(assigned_avg_rt_ms, (int, float))
                                    else None
                                ),
                                success_rate=(
                                    float(assigned_success_rate)
                                    if isinstance(assigned_success_rate, (int, float))
                                    else None
                                ),
                            )
                        ),
                        status_reason=(
                            postprocessor_status_reason
                            if is_postprocessor
                            else task_status_reason
                        ),
                        postprocessor_kind=postprocessor_kind,
                        postprocessor_seconds=postprocessor_seconds,
                    )
                )
        return records

    def _infer_record_status(
        self, round_status: Optional[PlanRunStatus], logic_type: str
    ) -> str:
        if round_status == PlanRunStatus.SUCCEEDED:
            return (
                "succeeded"
                if logic_type == PlanStageItemType.POSTPROCESSOR.value
                else "completed"
            )
        if round_status == PlanRunStatus.FAILED:
            return "failed"
        if round_status == PlanRunStatus.STOPPED:
            return "stopped"
        return "pending"

    def _resolve_postprocessor_descriptor(
        self, run_params: Any
    ) -> tuple[Optional[str], Optional[int]]:
        seconds = self._coerce_seconds_from_payload(run_params)
        if seconds is None:
            return None, None
        return ("sleep" if seconds > 0 else "noop"), seconds

    def _infer_postprocessor_record_status(
        self,
        *,
        round_status: Optional[PlanRunStatus],
        selected_round: int,
        collection_id: int,
        status_markers: dict[str, str],
    ) -> tuple[str, str]:
        marker_action = status_markers.get("postprocessor")
        marker_round = self._coerce_int(status_markers.get("postprocessor_round"))
        marker_item = self._coerce_int(status_markers.get("postprocessor_item"))
        marker_matches = marker_round == selected_round and marker_item == collection_id

        if marker_matches:
            if marker_action in {"sleep_completed", "skipped_final_interval", "noop"}:
                return "succeeded", "已完成"
            if marker_action == "sleep_started":
                return "running", "等待中"
            if marker_action == "sleep_interrupted":
                if round_status == PlanRunStatus.STOPPED:
                    return "stopped", "已停止"
                return "failed", "已中断"

        inferred = self._infer_record_status(
            round_status, PlanStageItemType.POSTPROCESSOR.value
        )
        return inferred, self._label_plan_run_status(inferred)

    def _build_postprocessor_status_reason(
        self,
        *,
        status: Optional[str],
        seconds: Optional[int],
        status_markers: dict[str, str],
        marker_matches: bool = True,
        plan_run_status_detail: Optional[str],
    ) -> Optional[str]:
        if seconds is None:
            return None
        marker_action = status_markers.get("postprocessor") if marker_matches else None
        prefix = f"sleep {seconds}s"
        detail = str(plan_run_status_detail or "")
        if marker_action == "sleep_completed":
            return f"{prefix} 已完成。"
        if marker_action == "skipped_final_interval":
            return f"{prefix} 是最后一轮间隔，已按规则跳过。"
        if marker_action == "noop":
            return "后置处理无等待时间。"
        if marker_action == "sleep_started":
            return f"{prefix} 正在等待；该等待属于轮间间隔，不绑定 Run。"
        if marker_action == "sleep_interrupted":
            if "timeout_watchdog" in detail:
                return f"{prefix} 被 timeout watchdog 中断。"
            if "user_stopped" in detail or "stop_requested" in detail:
                return f"{prefix} 被人工停止中断。"
            if (
                status == "stopped"
                or "terminal_reconciled:stopped_run_terminal" in detail
                or "stopped_from_child_run" in detail
            ):
                return f"{prefix} 被停止中断。"
            if "stale_active_recovered:active_state_lost" in detail:
                return f"{prefix} 被 stale active recovery 中断。"
            return f"{prefix} 被执行状态变化中断。"
        if status == "stopped":
            return f"{prefix} 未完成；批次已停止。"
        if status == "failed":
            launch_failed_reason = status_markers.get("launch_failed_reason")
            if launch_failed_reason:
                return f"{prefix} 未执行；前置任务启动失败：{launch_failed_reason}。"
            if "stale_active_recovered:active_state_lost" in detail:
                return f"{prefix} 未完成；批次被 stale active recovery 判定为 active_state_lost。"
            return f"{prefix} 未完成；请先检查批次状态详情。"
        if status == "pending":
            return f"{prefix} 尚未开始。"
        return None

    def _build_unbound_task_status_reason(
        self,
        *,
        status: Optional[str],
        status_markers: dict[str, str],
        plan_run_status_detail: Optional[str],
    ) -> Optional[str]:
        if status != "failed":
            return None
        launch_failed_reason = status_markers.get("launch_failed_reason")
        if launch_failed_reason:
            return f"启动 Run 失败：{launch_failed_reason}。"
        detail = str(plan_run_status_detail or "")
        if "launch_failed" in detail:
            return "启动 Run 失败；请检查执行节点资源或任务配置。"
        if "incomplete_plan_execution" in detail:
            return "批次未完成全部预期 Run，请检查前序轮次或调度日志。"
        return None

    @staticmethod
    def _build_postprocessor_metric_summary(
        *,
        status: Optional[str],
        seconds: Optional[int],
        reason: Optional[str],
    ) -> Optional[str]:
        if seconds is None:
            return reason
        status_label = {
            "succeeded": "已完成",
            "running": "等待中",
            "failed": "已中断",
            "stopped": "已停止",
            "pending": "待处理",
        }.get(str(status or ""), str(status or "待处理"))
        summary = f"sleep {seconds}s · {status_label}"
        if reason:
            summary = f"{summary} · {reason}"
        return summary

    @staticmethod
    def _coerce_int(value: Any) -> Optional[int]:
        try:
            if value is None:
                return None
            return int(float(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _coerce_float(value: Any) -> Optional[float]:
        try:
            if value is None or isinstance(value, bool):
                return None
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    def _build_execution_summary(
        self,
        records: list[PlanRunTaskExecRecord],
        launched_run_ids: list[int],
    ) -> PlanRunExecutionSummary:
        succeeded_total = 0
        failed_total = 0
        stopped_total = 0
        pending_total = 0
        task_total = 0
        postprocessor_total = 0
        total_throughput = 0.0
        weighted_failed_requests = 0.0
        weighted_total_requests = 0
        max_avg_rt_ms: Optional[float] = None
        max_p95_rt_ms: Optional[float] = None
        max_p99_rt_ms: Optional[float] = None

        for record in records:
            status = (record.status or "pending").lower()
            if record.logic_type == PlanStageItemType.POSTPROCESSOR.value:
                postprocessor_total += 1
            else:
                task_total += 1

            if status in {"succeeded", "completed"}:
                succeeded_total += 1
            elif status == "failed":
                failed_total += 1
            elif status == "stopped":
                stopped_total += 1
            elif status == "pending":
                pending_total += 1

            if record.logic_type == PlanStageItemType.POSTPROCESSOR.value:
                continue

            if record.rps is not None:
                total_throughput += float(record.rps)
            if record.avg_rt_ms is not None:
                max_avg_rt_ms = (
                    float(record.avg_rt_ms)
                    if max_avg_rt_ms is None
                    else max(max_avg_rt_ms, float(record.avg_rt_ms))
                )
            if record.p95_rt_ms is not None:
                max_p95_rt_ms = (
                    float(record.p95_rt_ms)
                    if max_p95_rt_ms is None
                    else max(max_p95_rt_ms, float(record.p95_rt_ms))
                )
            if record.p99_rt_ms is not None:
                max_p99_rt_ms = (
                    float(record.p99_rt_ms)
                    if max_p99_rt_ms is None
                    else max(max_p99_rt_ms, float(record.p99_rt_ms))
                )

            if record.total_requests is None:
                continue
            weighted_total_requests += int(record.total_requests)
            if record.error_rate is not None:
                weighted_failed_requests += float(record.total_requests) * float(
                    record.error_rate
                )
            elif record.success_rate is not None:
                weighted_failed_requests += float(record.total_requests) * max(
                    0.0, min(1.0, 1 - float(record.success_rate))
                )

        return PlanRunExecutionSummary(
            total_records=len(records),
            task_total=task_total,
            postprocessor_total=postprocessor_total,
            succeeded_total=succeeded_total,
            failed_total=failed_total,
            stopped_total=stopped_total,
            pending_total=pending_total,
            launched_run_total=len(launched_run_ids),
            total_throughput=(
                round(total_throughput, 4) if total_throughput > 0 else None
            ),
            weighted_error_rate=(
                round(weighted_failed_requests / weighted_total_requests, 6)
                if weighted_total_requests > 0
                else None
            ),
            max_avg_rt_ms=(
                round(max_avg_rt_ms, 4) if max_avg_rt_ms is not None else None
            ),
            max_p95_rt_ms=(
                round(max_p95_rt_ms, 4) if max_p95_rt_ms is not None else None
            ),
            max_p99_rt_ms=(
                round(max_p99_rt_ms, 4) if max_p99_rt_ms is not None else None
            ),
        )

    def _attach_mixed_run_peak_qps(
        self,
        *,
        execution_summary: Optional[PlanRunExecutionSummary],
        records: list[PlanRunTaskExecRecord],
        plan: Optional[Plan],
    ) -> None:
        if execution_summary is None:
            return
        if getattr(plan, "domain_type", None) != "mixed_run":
            return
        peak_qps = self._resolve_mixed_run_peak_qps(
            records=records,
            fallback_total_tps=execution_summary.total_throughput,
        )
        if peak_qps is not None:
            execution_summary.peak_qps = peak_qps

    def _resolve_mixed_run_peak_qps(
        self,
        *,
        records: list[PlanRunTaskExecRecord],
        fallback_total_tps: Optional[float],
    ) -> Optional[float]:
        run_ids = [
            int(record.run_id)
            for record in records
            if isinstance(record.run_id, int) and record.run_id > 0
        ]
        if not run_ids:
            return fallback_total_tps

        points_by_ts: dict[str, float] = {}
        run_service = RunService(self.db)
        for run_id in sorted(set(run_ids)):
            try:
                trends = run_service.get_endpoint_trends(
                    run_id, metric="throughput", step_seconds=5
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "mixed_run peak qps trend lookup failed run_id=%s: %s",
                    run_id,
                    exc,
                )
                continue
            for item in getattr(trends, "items", None) or []:
                metric = getattr(item, "metric", None)
                metric_value = getattr(metric, "value", metric)
                if str(metric_value) != "throughput":
                    continue
                for point in getattr(item, "points", None) or []:
                    ts = getattr(point, "ts", None)
                    value = getattr(point, "value", None)
                    if not isinstance(value, (int, float)) or isinstance(value, bool):
                        continue
                    if not math.isfinite(float(value)):
                        continue
                    ts_key = ts.isoformat() if isinstance(ts, datetime) else str(ts)
                    points_by_ts[ts_key] = round(
                        points_by_ts.get(ts_key, 0.0) + float(value),
                        4,
                    )

        candidates: list[float] = []
        if points_by_ts:
            candidates.append(max(points_by_ts.values()))
        if isinstance(fallback_total_tps, (int, float)) and fallback_total_tps > 0:
            candidates.append(float(fallback_total_tps))
        return round(max(candidates), 4) if candidates else None

    def _build_selected_run_summary(
        self,
        records: list[PlanRunTaskExecRecord],
        selected_collection_id: Optional[int] = None,
    ) -> Optional[PlanRunSelectedRunSummary]:
        selected_record = self._find_selected_task_record(
            records, selected_collection_id
        )
        selected_run_id = selected_record.run_id if selected_record else None
        if selected_run_id is None:
            return None
        run = self.db.query(Run).filter(Run.run_id == selected_run_id).first()
        if not run:
            return None
        engine_type = (
            run.engine_type.value
            if hasattr(run.engine_type, "value")
            else run.engine_type
        )
        resolved_total_requests = (
            selected_record.total_requests
            if selected_record.total_requests is not None
            else getattr(run, "total_requests", None)
        )
        resolved_rps = (
            selected_record.rps
            if selected_record.rps is not None
            else getattr(run, "rps", None)
        )
        resolved_avg_rt_ms = (
            selected_record.avg_rt_ms
            if selected_record.avg_rt_ms is not None
            else getattr(run, "avg_rt_ms", None)
        )
        resolved_error_rate = (
            selected_record.error_rate
            if selected_record.error_rate is not None
            else getattr(run, "error_rate", None)
        )
        resolved_p95_rt_ms = (
            selected_record.p95_rt_ms
            if selected_record.p95_rt_ms is not None
            else getattr(run, "p95_rt_ms", None)
        )
        resolved_p99_rt_ms = (
            selected_record.p99_rt_ms
            if selected_record.p99_rt_ms is not None
            else getattr(run, "p99_rt_ms", None)
        )
        resolved_success_rate = (
            selected_record.success_rate
            if selected_record.success_rate is not None
            else getattr(run, "success_rate", None)
        )
        return PlanRunSelectedRunSummary(
            run_id=run.run_id,
            task_name=run.task_name,
            status=(
                run.run_status.value
                if hasattr(run.run_status, "value")
                else str(run.run_status)
            ),
            status_label=self._label_plan_run_status(
                run.run_status.value
                if hasattr(run.run_status, "value")
                else str(run.run_status)
            ),
            env=run.env,
            engine_type=engine_type,
            duration_seconds=self._resolve_duration_seconds(
                run.duration_seconds,
                run.started_at,
                run.ended_at,
            ),
            total_requests=resolved_total_requests,
            rps=resolved_rps,
            error_rate=resolved_error_rate,
            avg_rt_ms=resolved_avg_rt_ms,
            p95_rt_ms=resolved_p95_rt_ms,
            p99_rt_ms=resolved_p99_rt_ms,
            success_rate=resolved_success_rate,
            started_at=run.started_at,
            ended_at=run.ended_at,
            metric_summary=self._build_metric_summary(
                duration_seconds=self._resolve_duration_seconds(
                    run.duration_seconds,
                    run.started_at,
                    run.ended_at,
                ),
                total_requests=resolved_total_requests,
                rps=resolved_rps,
                avg_rt_ms=resolved_avg_rt_ms,
                success_rate=resolved_success_rate,
            ),
        )

    def _build_selected_task_summary(
        self,
        records: list[PlanRunTaskExecRecord],
        selected_collection_id: Optional[int],
    ) -> Optional[PlanRunSelectedTaskSummary]:
        selected_record = self._find_selected_task_record(
            records, selected_collection_id
        )
        if selected_record is None:
            return None
        record_index = next(
            (
                index + 1
                for index, record in enumerate(records)
                if record.collection_id == selected_record.collection_id
            ),
            None,
        )

        run = None
        if selected_record.run_id is not None:
            run = (
                self.db.query(Run).filter(Run.run_id == selected_record.run_id).first()
            )

        engine_type = None
        if run and getattr(run, "engine_type", None) is not None:
            engine_type = (
                run.engine_type.value
                if hasattr(run.engine_type, "value")
                else str(run.engine_type)
            )

        return PlanRunSelectedTaskSummary(
            collection_id=selected_record.collection_id,
            record_index=record_index,
            record_total=len(records),
            position_label=(
                f"第 {record_index} / {len(records)} 项"
                if record_index is not None
                else None
            ),
            stage=selected_record.stage,
            logic_type=selected_record.logic_type,
            logic_type_label=self._label_task_logic_type(selected_record.logic_type),
            task_id=selected_record.task_id,
            task_name=selected_record.task_name,
            run_id=selected_record.run_id,
            status=selected_record.status,
            status_label=selected_record.status_label
            or self._label_plan_run_status(selected_record.status),
            started_at=selected_record.started_at,
            ended_at=selected_record.ended_at,
            duration_seconds=self._resolve_duration_seconds(
                run.duration_seconds if run else None,
                selected_record.started_at,
                selected_record.ended_at,
            ),
            run_params=selected_record.run_params,
            env=run.env if run else None,
            engine_type=engine_type,
            total_requests=(
                selected_record.total_requests
                if selected_record.total_requests is not None
                else (getattr(run, "total_requests", None) if run else None)
            ),
            rps=(
                selected_record.rps
                if selected_record.rps is not None
                else (getattr(run, "rps", None) if run else None)
            ),
            error_rate=(
                selected_record.error_rate
                if selected_record.error_rate is not None
                else (getattr(run, "error_rate", None) if run else None)
            ),
            avg_rt_ms=(
                selected_record.avg_rt_ms
                if selected_record.avg_rt_ms is not None
                else (getattr(run, "avg_rt_ms", None) if run else None)
            ),
            p95_rt_ms=(
                selected_record.p95_rt_ms
                if selected_record.p95_rt_ms is not None
                else (getattr(run, "p95_rt_ms", None) if run else None)
            ),
            p99_rt_ms=(
                selected_record.p99_rt_ms
                if selected_record.p99_rt_ms is not None
                else (getattr(run, "p99_rt_ms", None) if run else None)
            ),
            success_rate=(
                selected_record.success_rate
                if selected_record.success_rate is not None
                else (getattr(run, "success_rate", None) if run else None)
            ),
            metric_summary=(
                selected_record.metric_summary
                if selected_record.logic_type == PlanStageItemType.POSTPROCESSOR.value
                else self._build_metric_summary(
                    duration_seconds=self._resolve_duration_seconds(
                        run.duration_seconds if run else None,
                        selected_record.started_at,
                        selected_record.ended_at,
                    ),
                    total_requests=(
                        selected_record.total_requests
                        if selected_record.total_requests is not None
                        else (run.total_requests if run else None)
                    ),
                    rps=(
                        selected_record.rps
                        if selected_record.rps is not None
                        else (run.rps if run else None)
                    ),
                    avg_rt_ms=(
                        selected_record.avg_rt_ms
                        if selected_record.avg_rt_ms is not None
                        else (run.avg_rt_ms if run else None)
                    ),
                    success_rate=(
                        selected_record.success_rate
                        if selected_record.success_rate is not None
                        else (run.success_rate if run else None)
                    ),
                )
            ),
            status_reason=selected_record.status_reason,
            postprocessor_kind=selected_record.postprocessor_kind,
            postprocessor_seconds=selected_record.postprocessor_seconds,
        )

    def _build_selected_interface_summary(
        self,
        records: list[PlanRunTaskExecRecord],
        selected_collection_id: Optional[int],
    ) -> Optional[PlanRunSelectedInterfaceSummary]:
        selected_record = self._find_selected_task_record(
            records, selected_collection_id
        )
        if selected_record is None:
            return None

        if selected_record.run_id is None:
            return PlanRunSelectedInterfaceSummary(
                collection_id=selected_record.collection_id,
                run_id=None,
                task_name=selected_record.task_name,
                source="none",
                metric_total=0,
                endpoint_preview=[],
                empty_reason="selected_item_has_no_run",
            )

        run = self.db.query(Run).filter(Run.run_id == selected_record.run_id).first()
        if not run:
            return PlanRunSelectedInterfaceSummary(
                collection_id=selected_record.collection_id,
                run_id=selected_record.run_id,
                task_name=selected_record.task_name,
                source="none",
                metric_total=0,
                endpoint_preview=[],
                empty_reason="selected_run_missing",
            )

        params = run.params if isinstance(run.params, dict) else {}
        summary_items = self._extract_summary_metric_items(params)
        if summary_items:
            endpoint_preview = [
                endpoint_name
                for endpoint_name in (
                    str(item.get("endpoint_name") or "").strip()
                    for item in summary_items
                )
                if endpoint_name
            ][:3]
            total_requests = 0
            total_throughput = 0.0
            max_avg_rt_ms = None
            max_p95_rt_ms = None
            max_p99_rt_ms = None
            for item in summary_items:
                raw_total_requests = item.get("total_requests")
                if isinstance(raw_total_requests, (int, float)):
                    total_requests += int(raw_total_requests)
                raw_throughput = item.get("throughput")
                if isinstance(raw_throughput, (int, float)):
                    total_throughput += float(raw_throughput)
                raw_avg_rt_ms = item.get("avg_rt_ms")
                if isinstance(raw_avg_rt_ms, (int, float)):
                    max_avg_rt_ms = max(
                        float(raw_avg_rt_ms), max_avg_rt_ms or float(raw_avg_rt_ms)
                    )
                raw_p95_rt_ms = item.get("p95_rt_ms")
                if isinstance(raw_p95_rt_ms, (int, float)):
                    max_p95_rt_ms = max(
                        float(raw_p95_rt_ms), max_p95_rt_ms or float(raw_p95_rt_ms)
                    )
                raw_p99_rt_ms = item.get("p99_rt_ms")
                if isinstance(raw_p99_rt_ms, (int, float)):
                    max_p99_rt_ms = max(
                        float(raw_p99_rt_ms), max_p99_rt_ms or float(raw_p99_rt_ms)
                    )
            return PlanRunSelectedInterfaceSummary(
                collection_id=selected_record.collection_id,
                run_id=run.run_id,
                task_name=selected_record.task_name or run.task_name,
                source="seed",
                metric_total=len(summary_items),
                total_requests=total_requests,
                total_throughput=total_throughput,
                max_avg_rt_ms=max_avg_rt_ms,
                max_p95_rt_ms=max_p95_rt_ms,
                max_p99_rt_ms=max_p99_rt_ms,
                endpoint_preview=endpoint_preview,
                empty_reason=None,
            )

        if params.get("metrics_s3"):
            source = "archive"
            empty_reason = "metrics_available_on_demand"
        elif self._get_run_agent_context(run):
            source = "agent"
            empty_reason = "metrics_available_on_demand"
        else:
            source = "none"
            empty_reason = "selected_run_has_no_interface_metrics"

        return PlanRunSelectedInterfaceSummary(
            collection_id=selected_record.collection_id,
            run_id=run.run_id,
            task_name=selected_record.task_name or run.task_name,
            source=source,
            metric_total=0,
            total_requests=None,
            total_throughput=None,
            max_avg_rt_ms=None,
            max_p95_rt_ms=None,
            max_p99_rt_ms=None,
            endpoint_preview=[],
            empty_reason=empty_reason,
        )

    def _build_report_summary(
        self,
        records: list[PlanRunTaskExecRecord],
        selected_collection_id: Optional[int],
    ) -> PlanRunReportSummary:
        selected_record = self._find_selected_task_record(
            records, selected_collection_id
        )
        selected_is_postprocessor = (
            selected_record is not None
            and selected_record.logic_type == PlanStageItemType.POSTPROCESSOR.value
        )
        linked_records = [record for record in records if record.run_id is not None]
        if not linked_records:
            return PlanRunReportSummary(
                linked_run_total=0,
                ready_total=0,
                template_fallback_total=0,
                missing_total=0,
                conclusion_status="missing",
                conclusion_text="当前轮还没有已绑定 Run 的任务项，暂时不存在批次报告前门。",
                selected_run_status=(
                    "not_applicable"
                    if selected_is_postprocessor
                    else (
                        "missing"
                        if selected_record is not None
                        and selected_record.run_id is None
                        else None
                    )
                ),
                selected_run_message=(
                    "当前选中项为后置处理，不绑定 Run；无需生成 Run 报告。"
                    if selected_is_postprocessor
                    else (
                        "当前选中项没有关联 Run，暂无可下载报告。"
                        if selected_record is not None
                        and selected_record.run_id is None
                        else None
                    )
                ),
                items=[],
            )

        report_service = ReportService(self.db)
        items: list[PlanRunReportItem] = []
        ready_total = 0
        template_fallback_total = 0
        missing_total = 0
        selected_run_status = (
            "not_applicable"
            if selected_is_postprocessor
            else (
                "missing"
                if selected_record is not None and selected_record.run_id is None
                else None
            )
        )
        selected_run_report_id = None
        selected_run_report_name = None
        selected_run_message = (
            "当前选中项为后置处理，不绑定 Run；无需生成 Run 报告。"
            if selected_is_postprocessor
            else (
                "当前选中项没有关联 Run，暂无可下载报告。"
                if selected_record is not None and selected_record.run_id is None
                else None
            )
        )
        run_ids = [
            int(record.run_id) for record in linked_records if record.run_id is not None
        ]
        resolutions_by_run = report_service.resolve_download_frontdoors(run_ids)

        for record in linked_records:
            resolution = resolutions_by_run.get(int(record.run_id)) or {
                "status": "missing",
                "message": ReportService.MISSING_REPORT_MESSAGE,
                "report_id": None,
                "report_name": None,
            }
            status = str(resolution.get("status") or "missing")
            if status == "ready":
                ready_total += 1
            elif status == "template_fallback":
                template_fallback_total += 1
            else:
                missing_total += 1

            item = PlanRunReportItem(
                collection_id=record.collection_id,
                run_id=int(record.run_id),
                task_name=record.task_name,
                resolution_status=status,
                message=str(resolution.get("message") or ""),
                report_id=(
                    int(resolution["report_id"])
                    if resolution.get("report_id") is not None
                    else None
                ),
                report_name=(
                    str(resolution["report_name"])
                    if resolution.get("report_name") is not None
                    else None
                ),
            )
            items.append(item)

            if (
                selected_record is not None
                and selected_record.run_id is not None
                and int(selected_record.run_id) == int(record.run_id)
            ):
                selected_run_status = item.resolution_status
                selected_run_report_id = item.report_id
                selected_run_report_name = item.report_name
                selected_run_message = item.message

        linked_run_total = len(items)
        if ready_total == linked_run_total:
            conclusion_status = "ready"
            conclusion_text = f"当前轮 {linked_run_total} 个已绑定 Run 都存在兼容当前模板的报告，可按任务项逐个下载。"
        elif ready_total > 0:
            conclusion_status = "partial"
            conclusion_text = (
                f"当前轮 {linked_run_total} 个已绑定 Run 中，"
                f"{ready_total} 个可直接下载，"
                f"{template_fallback_total} 个仍需刷新模板，"
                f"{missing_total} 个缺少报告。"
            )
        elif template_fallback_total > 0:
            conclusion_status = "partial"
            conclusion_text = (
                f"当前轮 {linked_run_total} 个已绑定 Run 暂无兼容当前模板的报告，"
                f"其中 {template_fallback_total} 个仍需刷新模板，"
                f"{missing_total} 个缺少报告。"
            )
        else:
            conclusion_status = "missing"
            conclusion_text = (
                f"当前轮 {linked_run_total} 个已绑定 Run 都还没有可下载报告。"
            )

        return PlanRunReportSummary(
            linked_run_total=linked_run_total,
            ready_total=ready_total,
            template_fallback_total=template_fallback_total,
            missing_total=missing_total,
            conclusion_status=conclusion_status,
            conclusion_text=conclusion_text,
            selected_run_status=selected_run_status,
            selected_run_report_id=selected_run_report_id,
            selected_run_report_name=selected_run_report_name,
            selected_run_message=selected_run_message,
            items=items,
        )

    @staticmethod
    def _merge_plan_advisory_verdict(current: str, candidate: str) -> str:
        order = {"pass": 0, "warn": 1, "fail": 2}
        return (
            candidate if order.get(candidate, 99) > order.get(current, 99) else current
        )

    def _build_advisory_summary(
        self,
        *,
        execution_summary: Optional[PlanRunExecutionSummary],
        selected_round_summary: Optional[PlanRunSelectedRoundSummary],
        report_summary: Optional[PlanRunReportSummary],
        task_exec_records: list[PlanRunTaskExecRecord],
    ) -> PlanRunAdvisorySummary:
        verdict = "pass"
        input_sources = [
            "execution_summary",
            "selected_round_summary",
            "report_summary",
        ]
        key_findings: list[PlanRunAdvisoryFinding] = []
        recommended_actions: list[str] = []
        limitations: list[str] = []
        evidence_gaps: list[str] = []
        blocking_reasons: list[str] = []

        if execution_summary is None:
            return PlanRunAdvisorySummary(
                verdict="warn",
                delivery_status="blocked",
                delivery_status_label="证据不足",
                evidence_ready=False,
                summary_text="当前批次还没有稳定的执行摘要，暂时无法给出批次级结论。",
                input_sources=input_sources,
                key_findings=[],
                evidence_gaps=["execution_summary"],
                blocking_reasons=["当前缺少 execution_summary。"],
                recommended_actions=[
                    "先确认批次是否已产生执行明细，再回到批次详情查看。"
                ],
                limitations=["当前缺少 execution_summary。"],
            )

        status_label = (
            selected_round_summary.status_label
            if selected_round_summary and selected_round_summary.status_label
            else (
                self._label_plan_run_status(selected_round_summary.status)
                if selected_round_summary and selected_round_summary.status
                else "-"
            )
        )
        key_findings.append(
            PlanRunAdvisoryFinding(
                label="当前轮状态",
                detail=f"{selected_round_summary.label if selected_round_summary and selected_round_summary.label else '当前轮'} / {status_label}",
            )
        )
        key_findings.append(
            PlanRunAdvisoryFinding(
                label="执行摘要",
                detail=(
                    f"执行明细 {execution_summary.total_records}，"
                    f"任务项 {execution_summary.task_total}，"
                    f"成功 {execution_summary.succeeded_total}，"
                    f"失败 {execution_summary.failed_total}，"
                    f"待处理 {execution_summary.pending_total}。"
                ),
            )
        )

        performance_parts: list[str] = []
        if execution_summary.total_throughput is not None:
            performance_parts.append(f"总 TPS {execution_summary.total_throughput:.2f}")
        if execution_summary.weighted_error_rate is not None:
            performance_parts.append(
                f"加权错误率 {execution_summary.weighted_error_rate * 100:.2f}%"
            )
        if execution_summary.max_p95_rt_ms is not None:
            performance_parts.append(
                f"最差 P95 {execution_summary.max_p95_rt_ms:.2f} ms"
            )
        if execution_summary.max_p99_rt_ms is not None:
            performance_parts.append(
                f"最差 P99 {execution_summary.max_p99_rt_ms:.2f} ms"
            )
        if performance_parts:
            key_findings.append(
                PlanRunAdvisoryFinding(
                    label="性能摘要",
                    detail=" / ".join(performance_parts),
                )
            )

        if report_summary is not None:
            key_findings.append(
                PlanRunAdvisoryFinding(
                    label="报告前门",
                    detail=(
                        f"已绑定 Run {report_summary.linked_run_total}，"
                        f"可下载 {report_summary.ready_total}，"
                        f"待刷新模板 {report_summary.template_fallback_total}，"
                        f"缺报告 {report_summary.missing_total}。"
                    ),
                )
            )

        if selected_round_summary and selected_round_summary.status in {
            PlanRunStatus.PREPARING,
            PlanRunStatus.RUNNING,
        }:
            verdict = self._merge_plan_advisory_verdict(verdict, "warn")
            reason = "当前轮尚未终态，批次结论不能视为最终交付结论。"
            limitations.append(reason)
            blocking_reasons.append(reason)
            recommended_actions.append(
                "待当前轮进入终态后，再基于同一页的执行摘要和报告摘要做最终判断。"
            )

        if execution_summary.failed_total > 0:
            failed_postprocessor_total = sum(
                1
                for item in task_exec_records
                if item.logic_type == PlanStageItemType.POSTPROCESSOR.value
                and str(item.status or "").lower() == "failed"
            )
            verdict = self._merge_plan_advisory_verdict(verdict, "fail")
            if failed_postprocessor_total > 0:
                blocking_reasons.append(
                    f"当前轮存在 {failed_postprocessor_total} 个失败后置处理。"
                )
                recommended_actions.append(
                    "优先查看失败后置处理的 sleep 秒数与中断原因；后置处理不绑定 Run。"
                )
            if execution_summary.failed_total > failed_postprocessor_total:
                failed_run_total = (
                    execution_summary.failed_total - failed_postprocessor_total
                )
                blocking_reasons.append(f"当前轮存在 {failed_run_total} 个失败任务项。")
                recommended_actions.append(
                    "优先回钻失败任务项对应的 Run / 日志 / dashboard，确认失败是否集中在同一阶段或同一环境。"
                )
        elif execution_summary.stopped_total > 0:
            verdict = self._merge_plan_advisory_verdict(verdict, "warn")
            blocking_reasons.append(
                f"当前轮存在 {execution_summary.stopped_total} 个已停止项。"
            )
            recommended_actions.append(
                "当前轮存在已停止项，先确认是人工止损还是运行链路异常。"
            )

        if execution_summary.pending_total > 0:
            verdict = self._merge_plan_advisory_verdict(verdict, "warn")
            reason = "当前轮仍有待处理项，执行结果尚未完全收口。"
            limitations.append(reason)
            blocking_reasons.append(reason)

        if execution_summary.weighted_error_rate is not None:
            if execution_summary.weighted_error_rate >= 0.05:
                verdict = self._merge_plan_advisory_verdict(verdict, "fail")
                blocking_reasons.append(
                    f"当前轮加权错误率 {execution_summary.weighted_error_rate * 100:.2f}% 已达到失败级阈值。"
                )
                recommended_actions.append(
                    "当前轮加权错误率已达失败级，建议先核对错误率最高的 Run 与对应报告。"
                )
            elif execution_summary.weighted_error_rate >= 0.01:
                verdict = self._merge_plan_advisory_verdict(verdict, "warn")
                evidence_gaps.append("weighted_error_rate_needs_review")
                recommended_actions.append(
                    "当前轮加权错误率已有明显波动，建议优先看高错误率任务项的 RunDetail。"
                )

        if (
            execution_summary.max_p95_rt_ms is not None
            or execution_summary.max_p99_rt_ms is not None
        ):
            limitations.append(
                "当前结论只展示本轮最差 P95/P99；未绑定基线时只能说明波动，不能直接判定退化。"
            )
            evidence_gaps.append("baseline_comparison")

        if report_summary is not None:
            if report_summary.template_fallback_total > 0:
                verdict = self._merge_plan_advisory_verdict(verdict, "warn")
                evidence_gaps.append("template_fallback_report_template")
                blocking_reasons.append(
                    f"当前轮仍有 {report_summary.template_fallback_total} 个 Run 只有待刷新模板报告。"
                )
                recommended_actions.append(
                    "对仍需刷新模板的 Run 重新生成新版报告，避免批次报告入口停留在 template_fallback。"
                )
            if report_summary.missing_total > 0:
                verdict = self._merge_plan_advisory_verdict(verdict, "warn")
                evidence_gaps.append("missing_report")
                blocking_reasons.append(
                    f"当前轮仍有 {report_summary.missing_total} 个 Run 缺少报告。"
                )
                recommended_actions.append(
                    "优先为缺报告的 Run 补生成报告，再做批次级对外结论。"
                )
            if report_summary.ready_total > 0:
                recommended_actions.append(
                    "优先下载 ready 状态的报告，作为当前轮的最小交付证据。"
                )
            if report_summary.ready_total == 0:
                reason = "当前轮还没有 ready 状态的报告前门。"
                limitations.append(reason)
                evidence_gaps.append("ready_report_frontdoor")
        else:
            reason = "当前缺少 report_summary。"
            limitations.append(reason)
            evidence_gaps.append("report_summary")

        if execution_summary.total_records == 0:
            verdict = self._merge_plan_advisory_verdict(verdict, "warn")
            reason = "当前轮没有执行明细，批次结论仅能停留在空摘要。"
            limitations.append(reason)
            blocking_reasons.append(reason)

        if (
            report_summary is not None
            and report_summary.linked_run_total > 0
            and report_summary.ready_total == report_summary.linked_run_total
        ):
            recommended_actions.append(
                "当前轮所有已绑定 Run 都有 ready 报告；如执行状态和业务检查符合预期，可进入人工复核确认。"
            )

        evidence_ready = (
            verdict == "pass"
            and report_summary is not None
            and report_summary.linked_run_total > 0
            and report_summary.ready_total == report_summary.linked_run_total
            and execution_summary.failed_total == 0
            and execution_summary.stopped_total == 0
            and execution_summary.pending_total == 0
        )
        if evidence_ready:
            delivery_status = "ready_for_review"
            delivery_status_label = "可人工复核"
        elif blocking_reasons:
            delivery_status = "blocked"
            delivery_status_label = "需先处理阻断项"
        else:
            delivery_status = "needs_attention"
            delivery_status_label = "需补齐证据"

        summary_map = {
            "pass": "当前轮执行和报告前门均已基本收口，可作为批次通过候选；仍需人工复核后确认。",
            "warn": "当前轮已形成初步批次结论，但仍有报告、状态或质量残项需要复核。",
            "fail": "当前轮存在失败级信号，建议先按失败任务项和报告缺口处理，再谈批次结论。",
        }

        if not recommended_actions:
            recommended_actions.append(
                "先打开批次详情中的可下载报告与对应 Run，确认当前轮是否满足最小交付证据。"
            )

        primary_focus = self._build_plan_run_advisory_primary_focus(
            execution_summary=execution_summary,
            report_summary=report_summary,
            task_exec_records=task_exec_records,
        )

        return PlanRunAdvisorySummary(
            verdict=verdict,
            delivery_status=delivery_status,
            delivery_status_label=delivery_status_label,
            evidence_ready=evidence_ready,
            summary_text=summary_map.get(verdict, "当前轮批次结论未知"),
            input_sources=input_sources,
            primary_focus=primary_focus,
            key_findings=key_findings,
            evidence_gaps=list(dict.fromkeys(evidence_gaps)),
            blocking_reasons=list(dict.fromkeys(blocking_reasons)),
            recommended_actions=recommended_actions,
            limitations=list(dict.fromkeys(limitations)),
        )

    @staticmethod
    def _build_plan_run_advisory_primary_focus(
        *,
        execution_summary: PlanRunExecutionSummary,
        report_summary: Optional[PlanRunReportSummary],
        task_exec_records: list[PlanRunTaskExecRecord],
    ) -> Optional[PlanRunAdvisoryPrimaryFocus]:
        if execution_summary.failed_total > 0:
            failed_record = next(
                (
                    item
                    for item in task_exec_records
                    if str(item.status or "").lower() == "failed"
                ),
                None,
            )
            if failed_record is not None:
                if failed_record.logic_type == PlanStageItemType.POSTPROCESSOR.value:
                    return PlanRunAdvisoryPrimaryFocus(
                        kind="failed_postprocessor",
                        label="先看失败后置处理",
                        detail=(
                            failed_record.status_reason
                            or f"{failed_record.task_name or '后置处理'} 已失败；该项不绑定 Run，请检查 sleep 中断原因。"
                        ),
                        collection_id=failed_record.collection_id,
                    )
                return PlanRunAdvisoryPrimaryFocus(
                    kind="failed_run",
                    label="先看失败任务项",
                    detail=(
                        failed_record.status_reason
                        or (
                            f"{failed_record.task_name or '当前任务项'} 已失败，"
                            "优先回钻对应 Run / 日志定位失败原因。"
                        )
                    ),
                    collection_id=failed_record.collection_id,
                    run_id=failed_record.run_id,
                )

        if report_summary is None:
            return None

        first_ready = next(
            (
                item
                for item in report_summary.items
                if item.resolution_status == "ready" and item.report_id is not None
            ),
            None,
        )
        if first_ready is not None:
            return PlanRunAdvisoryPrimaryFocus(
                kind="ready_report",
                label="先下载首个可交付报告",
                detail=(
                    f"{first_ready.task_name or '当前任务项'} 已有 ready 报告，"
                    "可先作为当前轮的最小交付证据。"
                ),
                collection_id=first_ready.collection_id,
                run_id=first_ready.run_id,
                report_id=first_ready.report_id,
            )

        first_missing = next(
            (
                item
                for item in report_summary.items
                if item.resolution_status == "missing"
            ),
            None,
        )
        if first_missing is not None:
            return PlanRunAdvisoryPrimaryFocus(
                kind="missing_report",
                label="先补缺报告项",
                detail=(
                    f"{first_missing.task_name or '当前任务项'} 还缺报告，"
                    "先定位该任务项并补生成报告。"
                ),
                collection_id=first_missing.collection_id,
                run_id=first_missing.run_id,
            )

        first_template_fallback = next(
            (
                item
                for item in report_summary.items
                if item.resolution_status == "template_fallback"
            ),
            None,
        )
        if first_template_fallback is not None:
            return PlanRunAdvisoryPrimaryFocus(
                kind="template_fallback_report",
                label="先处理待刷新模板报告",
                detail=(
                    f"{first_template_fallback.task_name or '当前任务项'} 仍需刷新模板，"
                    "建议先定位并重生成为新版报告。"
                ),
                collection_id=first_template_fallback.collection_id,
                run_id=first_template_fallback.run_id,
            )

        return None

    def _resolve_selected_collection_id(
        self,
        selected_collection_id: Optional[int],
        records: list[PlanRunTaskExecRecord],
    ) -> Optional[int]:
        if selected_collection_id is not None:
            for record in records:
                if record.collection_id == selected_collection_id:
                    return selected_collection_id
        return records[0].collection_id if records else None

    def _find_selected_task_record(
        self,
        records: list[PlanRunTaskExecRecord],
        selected_collection_id: Optional[int],
    ) -> Optional[PlanRunTaskExecRecord]:
        if selected_collection_id is not None:
            for record in records:
                if record.collection_id == selected_collection_id:
                    return record
        return records[0] if records else None

    def _build_selected_round_summary(
        self,
        selected_round: int,
        rounds: list[PlanRunRoundItem],
        records: list[PlanRunTaskExecRecord],
        launched_run_ids: list[int],
    ) -> PlanRunSelectedRoundSummary:
        execution_summary = self._build_execution_summary(records, launched_run_ids)
        started_at_values = [
            record.started_at for record in records if record.started_at is not None
        ]
        ended_at_values = [
            record.ended_at for record in records if record.ended_at is not None
        ]
        round_item = next(
            (item for item in rounds if item.round == selected_round), None
        )
        return PlanRunSelectedRoundSummary(
            round=selected_round,
            label=round_item.label if round_item else f"轮次 {selected_round}",
            status=round_item.status if round_item else None,
            status_label=self._label_plan_run_status(
                round_item.status.value
                if round_item and hasattr(round_item.status, "value")
                else (
                    str(round_item.status)
                    if round_item and round_item.status is not None
                    else None
                )
            ),
            summary_label=(
                f"执行明细 {execution_summary.total_records} · "
                f"任务项 {execution_summary.task_total} · "
                f"后置处理 {execution_summary.postprocessor_total} · "
                f"已启动 Run {execution_summary.launched_run_total}"
            ),
            total_records=execution_summary.total_records,
            task_total=execution_summary.task_total,
            postprocessor_total=execution_summary.postprocessor_total,
            succeeded_total=execution_summary.succeeded_total,
            failed_total=execution_summary.failed_total,
            stopped_total=execution_summary.stopped_total,
            pending_total=execution_summary.pending_total,
            launched_run_total=execution_summary.launched_run_total,
            started_at=min(started_at_values) if started_at_values else None,
            ended_at=max(ended_at_values) if ended_at_values else None,
        )

    def _extract_summary_metric_items(
        self, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        summary_metrics = params.get("summary_metrics")
        if not isinstance(summary_metrics, list):
            return []
        return [item for item in summary_metrics if isinstance(item, dict)]

    def _extract_max_summary_metric_value(
        self, summary_items: list[dict[str, Any]], key: str
    ) -> Optional[float]:
        values: list[float] = []
        for item in summary_items:
            raw_value = item.get(key)
            if isinstance(raw_value, (int, float)):
                values.append(float(raw_value))
        return max(values) if values else None

    def _resolve_duration_seconds(
        self,
        explicit_duration: Optional[int],
        started_at: Optional[datetime],
        ended_at: Optional[datetime],
    ) -> Optional[int]:
        computed_duration = _compute_plan_run_duration_seconds(started_at, ended_at)
        if explicit_duration is None:
            return computed_duration
        normalized_duration = max(int(explicit_duration), 0)
        if (
            normalized_duration == 0
            and computed_duration is not None
            and computed_duration > 0
        ):
            return computed_duration
        return normalized_duration

    def _build_metric_summary(
        self,
        duration_seconds: Optional[int],
        total_requests: Optional[int],
        rps: Optional[float],
        avg_rt_ms: Optional[float],
        success_rate: Optional[float],
    ) -> Optional[str]:
        parts: list[str] = []
        if duration_seconds is not None:
            parts.append(f"耗时 {duration_seconds}s")
        if total_requests is not None:
            parts.append(f"总请求 {total_requests}")
        if rps is not None:
            parts.append(f"吞吐 {rps:g}")
        if avg_rt_ms is not None:
            parts.append(f"平均响应 {avg_rt_ms:g}ms")
        if success_rate is not None:
            parts.append(f"成功率 {success_rate * 100:.1f}%")
        return " · ".join(parts) if parts else None

    def _label_task_logic_type(self, logic_type: Optional[str]) -> Optional[str]:
        if logic_type == PlanStageItemType.POSTPROCESSOR.value:
            return "后置处理"
        if logic_type == PlanStageItemType.TASK.value:
            return "任务"
        return logic_type

    def _label_plan_status(self, status: Optional[str]) -> Optional[str]:
        if status == PlanStatus.READY.value:
            return "可执行"
        if status == PlanStatus.SUSPENDED.value:
            return "暂停"
        if status == PlanStatus.RUNNING.value:
            return "执行中"
        if status == PlanStatus.DELETED.value:
            return "已删除"
        return status

    def _label_plan_exec_type(self, exec_type: Optional[str]) -> Optional[str]:
        if exec_type == "manual":
            return "手动触发"
        if exec_type == "cron":
            return "周期执行"
        if exec_type == "fixed":
            return "单次定时执行"
        return exec_type

    def _label_round_progress(
        self,
        round_value: Optional[int],
        round_total: Optional[int],
    ) -> Optional[str]:
        if round_total is None or round_total <= 1:
            return "单轮"
        if round_value is None or round_value <= 0:
            return f"共 {round_total} 个轮次"
        return f"轮次 {round_value} / 共 {round_total}"

    def _label_round_mode(self, round_total: Optional[int]) -> Optional[str]:
        if round_total is None or round_total <= 1:
            return "单轮"
        return "轮次编排"

    def _label_plan_run_status(self, status: Optional[str]) -> Optional[str]:
        if status == PlanRunStatus.PREPARING.value:
            return "准备中"
        if status == PlanRunStatus.RUNNING.value:
            return "执行中"
        if status == PlanRunStatus.SUCCEEDED.value:
            return "已完成"
        if status == PlanRunStatus.FAILED.value:
            return "失败"
        if status == PlanRunStatus.STOPPED.value:
            return "已停止"
        if status == PlanRunStatus.SUSPENDED.value:
            return "暂停"
        if status == PlanRunStatus.DUPLICATED.value:
            return "重复执行"
        return status

    def _get_run_agent_context(self, run: Run) -> Optional[tuple[str, str]]:
        params = run.params if isinstance(run.params, dict) else {}
        token = (
            params.get("agent_run_token")
            or params.get("agent_token")
            or params.get("agent_session")
            or params.get("run_token")
        )
        host = params.get("agent_host") or params.get("agent_ip")
        if isinstance(host, str) and host and isinstance(token, str) and token:
            return host, token
        return None

    def _normalize_started_window(
        self,
        started_from: Optional[datetime],
        started_to: Optional[datetime],
    ) -> Tuple[Optional[datetime], Optional[datetime]]:
        started_from = self._as_naive_utc(started_from)
        started_to = self._as_naive_utc(started_to)
        if started_from and started_to and started_from > started_to:
            started_from, started_to = started_to, started_from
        if started_to is not None:
            started_to = started_to + (
                timedelta(seconds=1)
                if started_to.microsecond == 0
                else timedelta(microseconds=1)
            )
        return started_from, started_to

    def _as_naive_utc(self, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def _build_round_items(
        self,
        total_rounds: int,
        current_round: int,
        final_status: PlanRunStatus,
    ) -> list[PlanRunRoundItem]:
        rounds: list[PlanRunRoundItem] = []
        for round_index in range(1, max(total_rounds, 1) + 1):
            if round_index < current_round:
                status = PlanRunStatus.SUCCEEDED
            elif round_index == current_round:
                status = final_status
            else:
                status = PlanRunStatus.PREPARING
            rounds.append(
                PlanRunRoundItem(
                    round=round_index,
                    label=f"轮次 {round_index}",
                    status=status,
                    status_label=self._label_plan_run_status(
                        status.value if hasattr(status, "value") else str(status)
                    ),
                )
            )
        return rounds

    def _resolve_selected_round(
        self, selected_round: Optional[int], current_round: int, total_rounds: int
    ) -> int:
        if selected_round is None:
            return current_round
        return max(1, min(selected_round, max(total_rounds, 1)))

    def _count_task_items(self, stages: Any) -> int:
        if not isinstance(stages, list):
            return 0
        count = 0
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            items = stage.get("items")
            if not isinstance(items, list):
                continue
            for item in items:
                if (
                    isinstance(item, dict)
                    and item.get("type") == PlanStageItemType.TASK.value
                ):
                    count += 1
        return count

    def _count_postprocessor_items(self, stages: Any) -> int:
        if not isinstance(stages, list):
            return 0
        count = 0
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            items = stage.get("items")
            if not isinstance(items, list):
                continue
            for item in items:
                if (
                    isinstance(item, dict)
                    and item.get("type") == PlanStageItemType.POSTPROCESSOR.value
                ):
                    count += 1
        return count

    def _count_stages(self, stages: Any) -> int:
        if not isinstance(stages, list):
            return 0
        return len([stage for stage in stages if isinstance(stage, dict)])

    def _resolve_stage_item_for_round(
        self,
        item: dict[str, Any],
        round_number: int,
    ) -> dict[str, Any]:
        resolved = dict(item)
        round_overrides = item.get("round_overrides")
        if not isinstance(round_overrides, list):
            return resolved

        selected_override: Optional[dict[str, Any]] = None
        for override in round_overrides:
            if not isinstance(override, dict):
                continue
            try:
                override_round = int(override.get("round"))
            except (TypeError, ValueError):
                continue
            if override_round == round_number:
                selected_override = override
                break

        if selected_override is None:
            return resolved

        for field_name in ("run_params", "post_params"):
            override_value = selected_override.get(field_name)
            if not isinstance(override_value, dict):
                continue
            base_value = resolved.get(field_name)
            if isinstance(base_value, dict):
                merged_base = dict(base_value)
                if field_name == "post_params" and any(
                    key in override_value for key in _POSTPROCESSOR_SLEEP_KEYS
                ):
                    for key in _POSTPROCESSOR_SLEEP_KEYS:
                        merged_base.pop(key, None)
                resolved[field_name] = {**merged_base, **override_value}
            else:
                resolved[field_name] = dict(override_value)
        return resolved

    def _build_plan_execution_estimate(
        self,
        plan: Optional[Plan],
    ) -> Optional[PlanExecutionEstimate]:
        if plan is None:
            return None

        stages = plan.stages if isinstance(plan.stages, list) else []
        round_count = self._resolve_total_rounds(plan)
        task_duration_lookup = self._build_task_duration_lookup(stages)
        round_stage_totals: list[list[int]] = []
        round_task_totals: list[int] = []
        round_interval_totals: list[int] = []
        deterministic = True

        for round_number in range(1, max(round_count, 1) + 1):
            stage_totals: list[int] = []
            round_task_seconds = 0
            round_interval_seconds = 0
            for stage in self._sorted_stages(stages):
                stage_total = 0
                task_batch_seconds = 0
                items = stage.get("items") if isinstance(stage, dict) else None
                if not isinstance(items, list):
                    stage_totals.append(0)
                    continue
                for raw_item in items:
                    if not isinstance(raw_item, dict):
                        continue
                    item = self._resolve_stage_item_for_round(raw_item, round_number)
                    item_seconds, item_kind, item_deterministic = (
                        self._estimate_stage_item_seconds(
                            item,
                            task_duration_lookup=task_duration_lookup,
                        )
                    )
                    deterministic = deterministic and item_deterministic
                    if item_kind == "task":
                        # Stage tasks are dispatched together and run in parallel until a
                        # postprocessor boundary is reached, so wall-clock duration should
                        # use the slowest task in the current batch instead of summing all
                        # task durations serially.
                        task_batch_seconds = max(task_batch_seconds, item_seconds)
                    elif item_kind == "interval":
                        if task_batch_seconds > 0:
                            stage_total += task_batch_seconds
                            round_task_seconds += task_batch_seconds
                            task_batch_seconds = 0
                        stage_total += item_seconds
                        round_interval_seconds += item_seconds
                if task_batch_seconds > 0:
                    stage_total += task_batch_seconds
                    round_task_seconds += task_batch_seconds
                stage_totals.append(stage_total)
            round_stage_totals.append(stage_totals)
            round_task_totals.append(round_task_seconds)
            round_interval_totals.append(round_interval_seconds)

        baseline_stage_seconds = round_stage_totals[0] if round_stage_totals else []
        baseline_task_seconds = round_task_totals[0] if round_task_totals else 0
        baseline_interval_seconds = (
            round_interval_totals[0] if round_interval_totals else 0
        )
        baseline_single_round_seconds = sum(baseline_stage_seconds)
        total_seconds = sum(sum(stage_totals) for stage_totals in round_stage_totals)

        start_at = None
        if getattr(plan, "exec_type", None) == PlanExecType.FIXED:
            start_at = self._normalize_estimate_datetime(
                getattr(plan, "scheduled_at", None)
            )
        end_at = (
            start_at + timedelta(seconds=total_seconds)
            if start_at is not None
            else None
        )

        return PlanExecutionEstimate(
            round_count=max(round_count, 1),
            single_round_seconds=baseline_single_round_seconds,
            total_seconds=total_seconds,
            task_seconds=baseline_task_seconds,
            interval_seconds=baseline_interval_seconds,
            stage_seconds=baseline_stage_seconds,
            deterministic=deterministic,
            start_at=start_at,
            end_at=end_at,
        )

    @staticmethod
    def _resolve_plan_run_timeout_watchdog_grace_seconds() -> int:
        return max(0, PLAN_RUN_TIMEOUT_WATCHDOG_GRACE_SECONDS)

    @staticmethod
    def _resolve_child_run_timeout_watchdog_grace_seconds() -> int:
        raw = os.getenv("RUN_TIMEOUT_WATCHDOG_GRACE_SECONDS", "60")
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 60

    def _resolve_plan_run_timeout_seconds(self, plan: Optional[Plan]) -> Optional[int]:
        timeout_seconds = self._build_plan_execution_timeout_seconds(plan)
        if timeout_seconds is None or timeout_seconds <= 0:
            return None
        return int(timeout_seconds)

    def _build_plan_execution_timeout_seconds(
        self,
        plan: Optional[Plan],
    ) -> Optional[int]:
        if plan is None:
            return None

        stages = plan.stages if isinstance(plan.stages, list) else []
        round_count = self._resolve_total_rounds(plan)
        task_timeout_lookup = self._build_task_timeout_lookup(stages)
        round_stage_totals: list[list[int]] = []
        round_interval_totals: list[int] = []

        for round_number in range(1, max(round_count, 1) + 1):
            stage_totals: list[int] = []
            round_interval_seconds = 0
            for stage in self._sorted_stages(stages):
                stage_total = 0
                task_batch_seconds = 0
                items = stage.get("items") if isinstance(stage, dict) else None
                if not isinstance(items, list):
                    stage_totals.append(0)
                    continue
                for raw_item in items:
                    if not isinstance(raw_item, dict):
                        continue
                    item = self._resolve_stage_item_for_round(raw_item, round_number)
                    item_seconds, item_kind = self._estimate_stage_item_timeout_seconds(
                        item,
                        task_timeout_lookup=task_timeout_lookup,
                    )
                    if item_kind == "task":
                        task_batch_seconds = max(task_batch_seconds, item_seconds)
                    elif item_kind == "interval":
                        if task_batch_seconds > 0:
                            stage_total += task_batch_seconds
                            task_batch_seconds = 0
                        stage_total += item_seconds
                        round_interval_seconds += item_seconds
                if task_batch_seconds > 0:
                    stage_total += task_batch_seconds
                stage_totals.append(stage_total)
            round_stage_totals.append(stage_totals)
            round_interval_totals.append(round_interval_seconds)

        total_seconds = sum(sum(stage_totals) for stage_totals in round_stage_totals)
        return int(total_seconds) if total_seconds > 0 else None

    def _build_task_duration_lookup(self, stages: Any) -> dict[int, int]:
        task_ids: set[int] = set()
        for stage in self._sorted_stages(stages):
            items = stage.get("items") if isinstance(stage, dict) else None
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("type") != PlanStageItemType.TASK.value:
                    continue
                try:
                    task_id = int(item.get("task_id"))
                except (TypeError, ValueError):
                    continue
                if task_id > 0:
                    task_ids.add(task_id)
        if not task_ids:
            return {}
        task_rows = self._fetch_task_rows(task_ids)
        lookup: dict[int, int] = {}
        for row in task_rows:
            duration = getattr(row, "duration", None)
            if isinstance(duration, int) and duration >= 0:
                lookup[int(row.id)] = duration
        return lookup

    def _build_task_timeout_lookup(self, stages: Any) -> dict[int, Any]:
        task_ids: set[int] = set()
        for stage in self._sorted_stages(stages):
            items = stage.get("items") if isinstance(stage, dict) else None
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("type") != PlanStageItemType.TASK.value:
                    continue
                try:
                    task_id = int(item.get("task_id"))
                except (TypeError, ValueError):
                    continue
                if task_id > 0:
                    task_ids.add(task_id)
        if not task_ids:
            return {}
        return {int(row.id): row for row in self._fetch_task_rows(task_ids)}

    def _fetch_task_rows(self, task_ids: set[int]) -> list[Any]:
        if not task_ids:
            return []
        return self.db.query(Task).filter(Task.id.in_(sorted(task_ids))).all()

    def _estimate_stage_item_seconds(
        self,
        item: dict[str, Any],
        task_duration_lookup: dict[int, int],
    ) -> tuple[int, Optional[str], bool]:
        item_type = item.get("type")
        if item_type == PlanStageItemType.POSTPROCESSOR.value:
            seconds = self._coerce_seconds_from_payload(item.get("post_params"))
            return seconds if seconds is not None else 0, "interval", True
        if item_type != PlanStageItemType.TASK.value:
            return 0, None, True

        run_params = item.get("run_params")
        if isinstance(run_params, dict):
            duration = self._coerce_seconds_from_payload(
                run_params, keys=("duration", "duration_seconds")
            )
            if duration is not None:
                return duration, "task", True

        try:
            task_id = int(item.get("task_id"))
        except (TypeError, ValueError):
            task_id = None
        if task_id is not None and task_id in task_duration_lookup:
            return task_duration_lookup[task_id], "task", True
        return 0, "task", False

    def _estimate_stage_item_timeout_seconds(
        self,
        item: dict[str, Any],
        task_timeout_lookup: dict[int, Any],
    ) -> tuple[int, Optional[str]]:
        item_type = item.get("type")
        if item_type == PlanStageItemType.POSTPROCESSOR.value:
            seconds = self._coerce_seconds_from_payload(item.get("post_params"))
            return seconds if seconds is not None else 0, "interval"
        if item_type != PlanStageItemType.TASK.value:
            return 0, None

        try:
            task_id = int(item.get("task_id"))
        except (TypeError, ValueError):
            task_id = None
        task = task_timeout_lookup.get(task_id) if task_id is not None else None
        if task is None:
            duration = 0
            run_params = item.get("run_params")
            if isinstance(run_params, dict):
                coerced = self._coerce_seconds_from_payload(
                    run_params,
                    keys=("duration", "duration_seconds"),
                )
                duration = coerced if coerced is not None else 0
            return duration, "task"

        run_params = item.get("run_params")
        if not isinstance(run_params, dict):
            run_params = {}
        return self._resolve_task_item_timeout_seconds(task, run_params), "task"

    def _resolve_task_item_timeout_seconds(
        self,
        task: Any,
        run_params: dict[str, Any],
    ) -> int:
        from app.tasks.test_executor import (
            _build_task_dispatch_payload,
            _calculate_poll_timeout_seconds,
        )

        pseudo_run = SimpleNamespace(run_id=0, params=dict(run_params))
        task_data = _build_task_dispatch_payload(pseudo_run, task)
        return int(_calculate_poll_timeout_seconds(task_data, task))

    def _has_active_child_run_within_timeout_deadline(
        self,
        plan_run: PlanRun,
        current_now: datetime,
    ) -> bool:
        run_ids = getattr(
            plan_run,
            "launched_run_ids",
            None,
        ) or self._parse_launched_run_ids(getattr(plan_run, "status_detail", None))
        if not run_ids:
            return False

        active_runs = (
            self.db.query(Run)
            .filter(Run.run_id.in_(run_ids))
            .filter(Run.run_status.in_(list(_ACTIVE_RUN_STATUSES)))
            .all()
        )
        if not active_runs:
            return False

        task_ids = {
            int(run.task_id)
            for run in active_runs
            if getattr(run, "task_id", None) is not None
        }
        task_lookup = {int(row.id): row for row in self._fetch_task_rows(task_ids)}
        child_grace_seconds = self._resolve_child_run_timeout_watchdog_grace_seconds()
        for run in active_runs:
            task = (
                task_lookup.get(int(run.task_id))
                if getattr(run, "task_id", None) is not None
                else None
            )
            started_at = self._normalize_plan_run_datetime(
                getattr(run, "started_at", None)
            ) or self._normalize_plan_run_datetime(getattr(run, "created_at", None))
            if task is None or started_at is None:
                continue
            timeout_seconds = self._resolve_task_item_timeout_seconds(
                task,
                (
                    getattr(run, "params", None)
                    if isinstance(getattr(run, "params", None), dict)
                    else {}
                ),
            )
            deadline = started_at + timedelta(
                seconds=timeout_seconds + child_grace_seconds
            )
            if current_now < deadline:
                return True
        return False

    def _coerce_seconds_from_payload(
        self,
        payload: Any,
        keys: tuple[str, ...] = ("seconds", "sleep_time", "sleep_seconds"),
    ) -> Optional[int]:
        if not isinstance(payload, dict):
            return None
        for key in keys:
            raw_value = payload.get(key)
            try:
                if raw_value is None:
                    continue
                return max(0, int(float(raw_value)))
            except (TypeError, ValueError):
                continue
        return None

    def _normalize_estimate_datetime(
        self,
        value: Optional[datetime],
    ) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _sorted_stages(self, stages: Any) -> list[dict[str, Any]]:
        if not isinstance(stages, list):
            return []
        return sorted(
            [stage for stage in stages if isinstance(stage, dict)],
            key=lambda stage: self._stage_sort_value(stage),
        )

    def _stage_sort_value(self, raw_stage: dict[str, Any]) -> int:
        try:
            return int(raw_stage.get("stage", 0))
        except (TypeError, ValueError):
            return 0

    def _resolve_total_rounds(self, plan: Optional[Plan]) -> int:
        if (
            plan
            and plan.enable_round
            and isinstance(plan.total_round, int)
            and plan.total_round > 1
        ):
            return plan.total_round
        return 1

    def _can_stop_plan_run(self, status: PlanRunStatus) -> bool:
        return status in {PlanRunStatus.PREPARING, PlanRunStatus.RUNNING}

    def _parse_launched_run_ids(self, status_detail: Optional[str]) -> list[int]:
        if not status_detail:
            return []
        matched = re.search(r"launched_runs=([0-9,]+)", status_detail)
        if not matched:
            return []
        result: list[int] = []
        for part in matched.group(1).split(","):
            try:
                value = int(part.strip())
            except (TypeError, ValueError):
                continue
            if value > 0:
                result.append(value)
        return result

    def _ensure_plan_owner(self, plan: Optional[Plan], user_id: Optional[int]) -> None:
        if plan is None or user_id is None:
            return
        if (
            plan.created_by
            and int(plan.created_by) != int(user_id)
            and not self._is_plan_access_exempt_user(user_id)
        ):
            raise PermissionError("Forbidden: owner only")

    def _is_plan_access_exempt_user(self, user_id: int) -> bool:
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
