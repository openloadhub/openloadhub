"""
Plan 执行 Celery 任务

等待每个 Run 执行完成后再做 retry/on_failure 决策。
支持外部 stop：入口 + 每轮 item 前检查 PlanRun 是否已被标记为终态。
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from celery.schedules import crontab
from celery.result import AsyncResult

from app.core.celery_app import celery_app
import app.core.database as core_db
from app.models.plan import Plan
from app.models.plan_run import PlanRun
from app.models.run import Run
from app.models.task import Task
from app.services.plan_status_writer import (
    set_plan_status_without_touching_updated_at as _set_plan_status_without_touching_updated_at,
)
from app.services.notification_service import NotificationService
from common.schemas.run import RunCreate
from common.schemas.plan import (
    PlanRunK6BroadcastAcceptedResponse,
    PlanRunK6BroadcastRequest,
    PlanRunK6BroadcastResponse,
    PlanRunK6BroadcastTaskStatusResponse,
)
from common.models.enums import (
    PlanExecType,
    PlanRunStatus,
    PlanStageItemType,
    PlanStatus,
    RunStatus,
)

logger = logging.getLogger(__name__)

# Run 轮询配置
RUN_POLL_INTERVAL = int(os.getenv("PLAN_RUN_POLL_INTERVAL", "5"))  # 秒
RUN_POLL_TIMEOUT = int(os.getenv("PLAN_RUN_POLL_TIMEOUT", "1800"))  # 30 分钟

_TERMINAL_STATUSES = {
    PlanRunStatus.SUCCEEDED,
    PlanRunStatus.FAILED,
    PlanRunStatus.STOPPED,
}
_ACTIVE_PLAN_RUN_STATUSES = {
    PlanRunStatus.PREPARING,
    PlanRunStatus.RUNNING,
}

_TASK_VARIABLE_EXECUTION_CONTROL_KEYS = {
    "pod_count",
    "pod_num",
    "pod_total",
    "thread_count",
    "num_threads",
    "threads",
    "run_mode",
    "duration",
    "duration_seconds",
    "iterations",
    "request_count",
    "loops",
    "total_requests",
    "target_tps",
    "fixed_tps",
    "ramp_up",
    "protocol",
    "run_by",
}

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
)


def _start_run_execution_in_background(run_id: int) -> threading.Thread | None:
    from app.tasks.test_executor import execute_test_run_inline

    if os.environ.get("TESTING", "0") == "1":
        execute_test_run_inline(run_id)
        return None

    def _runner() -> None:
        try:
            execute_test_run_inline(run_id)
        except Exception:
            logger.exception(
                "Plan stage background execution failed for run %s", run_id
            )

    thread = threading.Thread(
        target=_runner,
        name=f"plan-stage-run-{run_id}",
        daemon=True,
    )
    thread.start()
    return thread


def _is_run_terminal(status: RunStatus) -> bool:
    return status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.STOPPED}


def _trigger_plan_run_terminal_webhooks(db, plan_run: PlanRun) -> None:
    try:
        NotificationService.trigger_plan_run_terminal_webhooks(
            db,
            plan_run,
            user_id=getattr(plan_run, "created_by", None),
        )
    except Exception:
        logger.exception(
            "PlanRun %d terminal webhook trigger failed",
            getattr(plan_run, "plan_run_id", -1),
        )


def _auto_generate_mixed_run_report_if_needed(db, plan_run: PlanRun) -> None:
    try:
        from app.services.mixed_run_report_service import MixedRunReportService

        MixedRunReportService(db).auto_generate_report_if_needed(
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
        db.rollback()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _compute_plan_run_duration_seconds(
    started_at: datetime | None, ended_at: datetime | None
) -> int | None:
    start_ts = _as_utc(started_at)
    end_ts = _as_utc(ended_at)
    if start_ts is None or end_ts is None:
        return None
    delta_seconds = (end_ts - start_ts).total_seconds()
    if delta_seconds <= 0:
        return 0
    return max(1, math.ceil(delta_seconds))


def _extract_plan_run_status_markers(status_detail: str | None) -> dict[str, str]:
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
    existing_status_detail: str | None, detail: str | None
) -> str | None:
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
        ordered_parts.extend(
            part.strip()
            for part in str(detail).split(";")
            if part.strip()
            and part.split("=", 1)[0].strip() not in _PLAN_RUN_STATUS_MARKER_KEYS
        )
    return ";".join(ordered_parts) if ordered_parts else None


def _build_execution_session_id(plan_run_id: int) -> str:
    return f"planrun-{int(plan_run_id)}"


def _attach_plan_run_scope_params(
    run_params: dict[str, Any],
    plan_run_id: int,
) -> dict[str, Any]:
    scoped = dict(run_params or {})
    scoped["plan_run_id"] = int(plan_run_id)
    scoped["mixed_run_id"] = int(plan_run_id)
    scoped["execution_session_id"] = _build_execution_session_id(plan_run_id)
    return scoped


def _build_launch_status_detail(
    *,
    plan_run_id: int,
    launch_waves_total: int,
    launched_run_ids: list[int],
    launch_failed_wave: int | None = None,
    launch_failed_reason: str | None = None,
) -> str:
    parts = [
        f"execution_session_id={_build_execution_session_id(plan_run_id)}",
        f"launch_waves_total={max(0, int(launch_waves_total))}",
    ]
    if launched_run_ids:
        parts.append(
            "launched_runs=" + ",".join(str(run_id) for run_id in launched_run_ids)
        )
    if launch_failed_wave is not None:
        parts.append(f"launch_failed_wave={int(launch_failed_wave)}")
    if launch_failed_reason:
        parts.append(
            f"launch_failed_reason={_format_status_detail_reason(launch_failed_reason)}"
        )
    return ";".join(parts)


def _format_status_detail_reason(reason: str, *, limit: int = 240) -> str:
    normalized = " ".join(str(reason or "").replace(";", ",").split())
    if len(normalized) > limit:
        return normalized[: limit - 3].rstrip() + "..."
    return normalized


def _resolve_plan_timezone(plan: Plan) -> timezone | ZoneInfo:
    timezone_name = (getattr(plan, "timezone", None) or "UTC").strip()
    if not timezone_name:
        return timezone.utc
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        logger.warning(
            "Plan %s has invalid timezone %r, fallback to UTC",
            getattr(plan, "plan_id", None),
            timezone_name,
        )
        return timezone.utc


def _resolve_scheduler_user_id(plan: Plan) -> int | None:
    created_by = getattr(plan, "created_by", None)
    if created_by is None:
        logger.warning(
            "Plan %s has no created_by, skip scheduled trigger",
            getattr(plan, "plan_id", None),
        )
        return None
    try:
        return int(created_by)
    except (TypeError, ValueError):
        logger.warning(
            "Plan %s has invalid created_by=%r, skip scheduled trigger",
            getattr(plan, "plan_id", None),
            created_by,
        )
        return None


def _get_latest_plan_run(db, plan_id: int) -> PlanRun | None:
    return (
        db.query(PlanRun)
        .filter(PlanRun.plan_id == plan_id)
        .order_by(PlanRun.plan_run_id.desc())
        .first()
    )


def _get_latest_scheduler_plan_run(
    db, plan_id: int, trigger_kind: str
) -> PlanRun | None:
    plan_runs = (
        db.query(PlanRun)
        .filter(PlanRun.plan_id == plan_id)
        .order_by(PlanRun.plan_run_id.desc())
        .all()
    )
    for plan_run in plan_runs:
        markers = _extract_plan_run_status_markers(
            getattr(plan_run, "status_detail", None)
        )
        if markers.get("scheduler") == trigger_kind:
            return plan_run
    return None


def _has_active_plan_run(db, plan_id: int) -> bool:
    return (
        db.query(PlanRun)
        .filter(
            PlanRun.plan_id == plan_id,
            PlanRun.status.in_(list(_ACTIVE_PLAN_RUN_STATUSES)),
        )
        .count()
        > 0
    )


def _build_crontab(expr: str, now: datetime, tzinfo: timezone | ZoneInfo) -> crontab:
    minute, hour, day_of_month, month_of_year, day_of_week = expr.split()
    return crontab(
        minute=minute,
        hour=hour,
        day_of_week=day_of_week,
        day_of_month=day_of_month,
        month_of_year=month_of_year,
        nowfun=lambda: now.astimezone(tzinfo),
    )


def _is_fixed_plan_due(plan: Plan, now: datetime) -> bool:
    scheduled_at = _as_utc(getattr(plan, "scheduled_at", None))
    return scheduled_at is not None and scheduled_at <= now


def _is_fixed_plan_already_consumed(
    plan: Plan,
    latest_plan_run: PlanRun | None,
) -> bool:
    scheduled_at = _as_utc(getattr(plan, "scheduled_at", None))
    if scheduled_at is None or latest_plan_run is None:
        return False
    latest_started_at = _as_utc(getattr(latest_plan_run, "started_at", None))
    latest_created_at = _as_utc(getattr(latest_plan_run, "created_at", None))
    reference = latest_started_at or latest_created_at
    if reference is None:
        return False
    return reference >= scheduled_at


def _is_cron_plan_due(plan: Plan, last_run_at: datetime | None, now: datetime) -> bool:
    cron_expr = (getattr(plan, "cron", None) or "").strip()
    if not cron_expr:
        return False
    try:
        tzinfo = _resolve_plan_timezone(plan)
        schedule = _build_crontab(cron_expr, now=now, tzinfo=tzinfo)
        reference = (
            _as_utc(last_run_at) or _as_utc(getattr(plan, "created_at", None)) or now
        )
        return bool(schedule.is_due(reference.astimezone(tzinfo)).is_due)
    except ValueError:
        logger.warning(
            "Plan %s has invalid cron expression %r, skip scheduled trigger",
            getattr(plan, "plan_id", None),
            cron_expr,
        )
        return False


def _build_initial_scheduled_task_batch(plan: Plan) -> list[dict[str, Any]] | None:
    stages = plan.stages if isinstance(plan.stages, list) else []

    def _stage_key(raw: Any) -> int:
        try:
            return int((raw or {}).get("stage", 0) or 0)
        except (AttributeError, TypeError, ValueError):
            return 0

    for stage in sorted(stages, key=_stage_key):
        items = stage.get("items") if isinstance(stage, dict) else None
        if not isinstance(items, list):
            continue
        task_batch: list[dict[str, Any]] = []
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            item = _resolve_stage_item_for_round(raw_item, 1)
            item_type = item.get("type")
            if item_type == PlanStageItemType.TASK.value:
                task_batch.append(item)
                continue
            if item_type == PlanStageItemType.POSTPROCESSOR.value:
                return task_batch or None
            if task_batch:
                return task_batch
        if task_batch:
            return task_batch
    return []


def _resolve_initial_scheduled_wave_requested_by_env(
    db,
    plan: Plan,
) -> dict[str, int] | None:
    task_batch = _build_initial_scheduled_task_batch(plan)
    if not task_batch:
        return {}

    from app.services.run_service import RunService

    requested_by_env: dict[str, int] = {}
    for item in task_batch:
        try:
            task_id = int(item.get("task_id") or 0)
        except (TypeError, ValueError):
            continue
        if task_id <= 0:
            continue
        task = db.query(Task).filter(Task.id == task_id).first()
        if task is None:
            return None
        run_params = item.get("run_params")
        if not isinstance(run_params, dict):
            run_params = {}
        requested = RunService._resolve_requested_pod_count(task, run_params)
        env = str(getattr(task, "env", "") or "default")
        requested_by_env[env] = requested_by_env.get(env, 0) + int(requested)
    return requested_by_env


def _has_capacity_for_initial_scheduled_wave(
    db,
    plan: Plan,
    reserved_by_env: dict[str, int] | None = None,
) -> bool:
    requested_by_env = _resolve_initial_scheduled_wave_requested_by_env(db, plan)
    if requested_by_env is None or not requested_by_env:
        return True

    from app.services.task_service import TaskService

    reservations = reserved_by_env if reserved_by_env is not None else {}
    task_service = TaskService(db)
    for env, requested in requested_by_env.items():
        try:
            pool = task_service._build_resource_pool(env)
        except Exception as exc:  # pragma: no cover - runtime discovery fallback
            logger.warning(
                "Skip capacity preflight for scheduled Plan %s env=%s: %s",
                getattr(plan, "plan_id", None),
                env,
                exc,
            )
            return True
        standby_total = max(0, int(getattr(pool, "standby_total", 0) or 0))
        idle_total = max(0, int(getattr(pool, "idle_total", 0) or 0))
        reserved = max(0, int(reservations.get(env, 0) or 0))
        requested_with_reserved = requested + reserved
        if requested_with_reserved > idle_total:
            logger.info(
                "Skip scheduled Plan %s because initial wave lacks agents: "
                "env=%s idle=%s standby=%s requested=%s reserved=%s",
                getattr(plan, "plan_id", None),
                env,
                idle_total,
                standby_total,
                requested,
                reserved,
            )
            return False
    if reserved_by_env is not None:
        for env, requested in requested_by_env.items():
            reserved_by_env[env] = max(0, int(reserved_by_env.get(env, 0) or 0)) + int(
                requested
            )
    return True


def _create_scheduled_plan_run(
    db,
    plan: Plan,
    user_id: int,
    trigger_kind: str,
    now: datetime,
) -> PlanRun:
    initial_round = 1 if _resolve_total_rounds(plan) > 1 else None
    plan_run = PlanRun(
        plan_id=plan.plan_id,
        plan_name=plan.name,
        status=PlanRunStatus.PREPARING,
        status_detail=f"scheduler={trigger_kind};preparing",
        stages_snapshot=plan.stages if isinstance(plan.stages, list) else [],
        started_at=now,
        round=initial_round,
        created_by=user_id,
    )
    db.add(plan_run)
    db.commit()
    db.refresh(plan_run)
    return plan_run


def _dispatch_plan_run_execution(plan_run_id: int, user_id: int) -> None:
    if os.environ.get("TESTING", "0") == "1":
        execute_plan_task(plan_run_id, user_id)
    else:
        execute_plan_task.delay(plan_run_id, user_id)


def _refresh_run_params_with_latest_task_variables(
    db,
    task_id: int,
    run_params: dict[str, Any],
) -> dict[str, Any]:
    """Keep plan/mixed execution controls while refreshing task script variables."""

    refreshed = dict(run_params or {})
    task = db.query(Task).filter(Task.id == int(task_id)).first()
    if task is None or not isinstance(getattr(task, "properties", None), dict):
        return refreshed

    raw_variables = task.properties.get("variables")
    if not isinstance(raw_variables, dict):
        return refreshed

    for raw_key, value in raw_variables.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            continue
        key = raw_key.strip()
        if key.lower() in _TASK_VARIABLE_EXECUTION_CONTROL_KEYS:
            continue
        refreshed[key] = value

    explicit_properties = refreshed.get("properties")
    if isinstance(explicit_properties, dict):
        merged_properties = dict(explicit_properties)
        merged_variables = (
            dict(merged_properties.get("variables"))
            if isinstance(merged_properties.get("variables"), dict)
            else {}
        )
        for raw_key, value in raw_variables.items():
            if not isinstance(raw_key, str) or not raw_key.strip():
                continue
            key = raw_key.strip()
            if key.lower() in _TASK_VARIABLE_EXECUTION_CONTROL_KEYS:
                continue
            merged_properties[key] = value
            merged_variables[key] = value
        if merged_variables:
            merged_properties["variables"] = merged_variables
        refreshed["properties"] = merged_properties

    return refreshed


def _launch_scheduled_plan_run(
    db,
    plan: Plan,
    trigger_kind: str,
    now: datetime,
) -> PlanRun | None:
    user_id = _resolve_scheduler_user_id(plan)
    if user_id is None:
        return None
    plan_run = _create_scheduled_plan_run(
        db=db,
        plan=plan,
        user_id=user_id,
        trigger_kind=trigger_kind,
        now=now,
    )
    _dispatch_plan_run_execution(plan_run.plan_run_id, user_id)
    return plan_run


def _collect_due_scheduled_plans(db, now: datetime) -> list[tuple[Plan, str]]:
    due_items: list[tuple[Plan, str]] = []
    reserved_by_env: dict[str, int] = {}
    plans = (
        db.query(Plan)
        .filter(
            Plan.status == PlanStatus.READY,
            Plan.exec_type.in_([PlanExecType.FIXED, PlanExecType.CRON]),
        )
        .order_by(Plan.plan_id.asc())
        .all()
    )

    for plan in plans:
        if _has_active_plan_run(db, plan.plan_id):
            continue

        latest_plan_run = _get_latest_plan_run(db, plan.plan_id)
        if plan.exec_type == PlanExecType.FIXED:
            latest_scheduler_run = _get_latest_scheduler_plan_run(
                db, plan.plan_id, PlanExecType.FIXED.value
            )
            if (
                _is_fixed_plan_due(plan, now)
                and not _is_fixed_plan_already_consumed(plan, latest_scheduler_run)
                and _has_capacity_for_initial_scheduled_wave(db, plan, reserved_by_env)
            ):
                due_items.append((plan, PlanExecType.FIXED.value))
            continue

        if (
            plan.exec_type == PlanExecType.CRON
            and _is_cron_plan_due(
                plan,
                last_run_at=getattr(
                    _get_latest_scheduler_plan_run(
                        db, plan.plan_id, PlanExecType.CRON.value
                    ),
                    "started_at",
                    None,
                ),
                now=now,
            )
            and _has_capacity_for_initial_scheduled_wave(db, plan, reserved_by_env)
        ):
            due_items.append((plan, PlanExecType.CRON.value))

    return due_items


@celery_app.task(bind=True, name="scan_scheduled_plans_task", max_retries=0)
def scan_scheduled_plans_task(self) -> dict[str, Any]:
    now = _utcnow()
    db = core_db.SessionLocal()
    try:
        launched: list[dict[str, int]] = []
        for plan, trigger_kind in _collect_due_scheduled_plans(db, now):
            plan_run = _launch_scheduled_plan_run(
                db=db,
                plan=plan,
                trigger_kind=trigger_kind,
                now=now,
            )
            if plan_run is None:
                continue
            launched.append(
                {
                    "plan_id": int(plan.plan_id),
                    "plan_run_id": int(plan_run.plan_run_id),
                }
            )

        return {
            "status": "ok",
            "scanned_at": now.isoformat(),
            "launched_total": len(launched),
            "launched": launched,
        }
    finally:
        db.close()


def _resolve_run_timeout_watchdog_grace_seconds() -> int:
    raw = os.getenv("RUN_TIMEOUT_WATCHDOG_GRACE_SECONDS", "60")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 60


def _govern_timed_out_active_runs(db, now: datetime) -> list[dict[str, int]]:
    from app.services.run_service import RunService
    from app.tasks.test_executor import _resolve_run_timeout_seconds
    from app.models.task import Task

    run_service = RunService(db)
    governed: list[dict[str, int]] = []

    for run in run_service.repo.find_active_runs():
        task = db.query(Task).filter(Task.id == run.task_id).first()
        started_at = _as_utc(getattr(run, "started_at", None)) or _as_utc(
            getattr(run, "created_at", None)
        )
        if task is None or started_at is None:
            continue

        timeout_seconds = _resolve_run_timeout_seconds(run, task)
        deadline = started_at + timedelta(
            seconds=timeout_seconds + _resolve_run_timeout_watchdog_grace_seconds()
        )
        if now < deadline:
            continue

        updated = run_service.govern_timed_out_run(run.run_id)
        if updated is not None:
            governed.append(
                {
                    "run_id": int(updated.run_id),
                    "timeout_seconds": int(timeout_seconds),
                }
            )

    return governed


@celery_app.task(bind=True, name="timeout_governance_watchdog_task", max_retries=0)
def timeout_governance_watchdog_task(self) -> dict[str, Any]:
    del self
    now = _utcnow()
    db = core_db.SessionLocal()
    try:
        from app.services.plan_service import PlanService

        plan_service = PlanService(db)
        governed_plan_runs = plan_service.govern_timed_out_active_plan_runs(now=now)
        governed_runs = _govern_timed_out_active_runs(db, now)
        return {
            "status": "ok",
            "scanned_at": now.isoformat(),
            "governed_run_total": len(governed_runs),
            "governed_plan_run_total": len(governed_plan_runs),
            "governed_runs": governed_runs,
            "governed_plan_runs": [
                int(plan_run.plan_run_id) for plan_run in governed_plan_runs
            ],
        }
    finally:
        db.close()


def _refresh_plan_run(db, plan_run_id: int) -> PlanRun | None:
    """从 DB 重新加载 PlanRun，检测外部 stop。"""
    db.expire_all()
    return db.query(PlanRun).filter(PlanRun.plan_run_id == plan_run_id).first()


def _wait_for_run(
    db,
    run_id: int,
    plan_run_id: int,
    timeout: int = RUN_POLL_TIMEOUT,
) -> RunStatus:
    """轮询等待 Run 到达终态。每轮也检查 PlanRun 是否被外部 stop。"""
    start = time.monotonic()

    # 获取一个新的 SessionLocal 供循环查询使用，防止外层长 session 缓存死锁
    while True:
        with core_db.SessionLocal() as poll_db:
            run = poll_db.query(Run).filter(Run.run_id == run_id).first()
            if not run:
                return RunStatus.FAILED
            if _is_run_terminal(run.run_status):
                # ★ 将终态数值返回（如果需要合并到外层db，外层再处理）
                return run.run_status

            # 检查是否被外部 stop
            pr = (
                poll_db.query(PlanRun)
                .filter(PlanRun.plan_run_id == plan_run_id)
                .first()
            )
            if pr and pr.status in _TERMINAL_STATUSES:
                logger.info(
                    "PlanRun %d externally stopped while waiting for run %d",
                    plan_run_id,
                    run_id,
                )
                return RunStatus.STOPPED

        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            logger.warning("Run %d poll timeout after %ds", run_id, timeout)
            return RunStatus.FAILED
        time.sleep(RUN_POLL_INTERVAL)


def _persist_run_ids(db, plan_run: PlanRun, run_ids: list[int]) -> None:
    """更新 PlanRun.launched_run_ids（合并已有值）。"""
    plan_run.launched_run_ids = list(run_ids)
    db.commit()


def _resolve_plan_run_end_ts(
    db,
    launched_run_ids: list[int],
    fallback: datetime,
) -> datetime:
    if not launched_run_ids:
        return fallback
    run = (
        db.query(Run)
        .filter(Run.run_id.in_(launched_run_ids))
        .order_by(Run.ended_at.desc(), Run.run_id.desc())
        .first()
    )
    ended_at = getattr(run, "ended_at", None) if run is not None else None
    if isinstance(ended_at, datetime):
        child_ended_at = _as_utc(ended_at)
        if child_ended_at is not None:
            return max(child_ended_at, fallback)
    return fallback


def _get_run_terminal_reason(db, run_id: int) -> str:
    run = db.query(Run).filter(Run.run_id == run_id).first()
    if run is None:
        return ""
    reason = getattr(run, "stop_reason", None) or getattr(
        run, "run_status_detail", None
    )
    return reason.strip() if isinstance(reason, str) else ""


def _is_alert_policy_stop_reason(reason: str | None) -> bool:
    return str(reason or "").strip().startswith("alert_policy:")


def _stop_plan_run_from_child_run(
    db,
    plan_run: PlanRun,
    *,
    run_id: int,
    launched_run_ids: list[int],
) -> bool:
    fresh_plan_run = _refresh_plan_run(db, plan_run.plan_run_id)
    if fresh_plan_run and fresh_plan_run.status in _TERMINAL_STATUSES:
        return fresh_plan_run.status == PlanRunStatus.STOPPED

    plan_run.status = PlanRunStatus.STOPPED
    plan_run.status_detail = _merge_plan_run_status_detail(
        plan_run.status_detail,
        f"stopped_from_child_run;run={run_id}",
    )
    plan_run.ended_at = _resolve_plan_run_end_ts(
        db,
        list(launched_run_ids) or [run_id],
        _utcnow(),
    )
    duration_seconds = _compute_plan_run_duration_seconds(
        getattr(plan_run, "started_at", None),
        plan_run.ended_at,
    )
    if duration_seconds is not None:
        plan_run.duration_seconds = duration_seconds
    db.commit()
    return True


def _resolve_total_rounds(plan: Plan) -> int:
    if plan.enable_round and isinstance(plan.total_round, int) and plan.total_round > 1:
        return plan.total_round
    return 1


def _count_task_items(stages: list[dict[str, Any]]) -> int:
    total = 0
    for stage in stages:
        items = stage.get("items") if isinstance(stage, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("type") != PlanStageItemType.TASK.value:
                continue
            try:
                task_id = int(item.get("task_id") or 0)
            except (TypeError, ValueError):
                continue
            if task_id > 0:
                total += 1
    return total


def _has_completed_all_expected_runs(
    *,
    stages: list[dict[str, Any]],
    total_rounds: int,
    launched_run_ids: list[int],
) -> bool:
    task_items_total = _count_task_items(stages)
    if task_items_total <= 0:
        return False
    expected_runs_total = task_items_total * max(total_rounds, 1)
    return len(launched_run_ids) >= expected_runs_total


def _resolve_stage_item_for_round(
    item: dict[str, Any],
    round_number: int,
) -> dict[str, Any]:
    resolved = dict(item)
    round_overrides = item.get("round_overrides")
    if not isinstance(round_overrides, list):
        return resolved

    selected_override: dict[str, Any] | None = None
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


def _sleep_with_stop_guard(plan_run_id: int, seconds: int) -> bool:
    remaining = max(0, seconds)
    while remaining > 0:
        with core_db.SessionLocal() as poll_db:
            plan_run = (
                poll_db.query(PlanRun)
                .filter(PlanRun.plan_run_id == plan_run_id)
                .first()
            )
            if plan_run and plan_run.status in _TERMINAL_STATUSES:
                return False
        time.sleep(1)
        remaining -= 1
    return True


def _resolve_postprocessor_sleep_seconds(item: dict[str, Any]) -> int:
    post_params = item.get("post_params") if isinstance(item, dict) else None
    if not isinstance(post_params, dict):
        return 0

    for key in _POSTPROCESSOR_SLEEP_KEYS:
        if key not in post_params:
            continue
        try:
            return max(0, int(float(post_params.get(key) or 0)))
        except (TypeError, ValueError):
            continue
    return 0


def _run_postprocessor(plan_run: PlanRun, item: dict[str, Any]) -> bool:
    sleep_seconds = _resolve_postprocessor_sleep_seconds(item)
    if sleep_seconds <= 0:
        return True

    logger.info(
        "PlanRun %d: executing sleep postprocessor for %ds",
        plan_run.plan_run_id,
        sleep_seconds,
    )
    return _sleep_with_stop_guard(plan_run.plan_run_id, sleep_seconds)


def _build_postprocessor_status_detail(
    *,
    round_number: int,
    stage_number: int,
    item_idx: int,
    action: str,
    seconds: int,
) -> str:
    return (
        f"postprocessor_round={round_number};"
        f"postprocessor_stage={stage_number};"
        f"postprocessor_item={item_idx};"
        f"postprocessor={action};"
        f"postprocessor_seconds={max(0, int(seconds))}"
    )


def _execute_stage_task_batch(
    *,
    db,
    plan_run: PlanRun,
    run_service,
    user_id: int,
    round_number: int,
    stage_number: int,
    task_batch: list[dict[str, Any]],
    launch_wave_index: int,
    launched_run_ids: list[int],
    failed_items: list[dict[str, Any]],
) -> tuple[bool, bool]:
    fresh_pr = _refresh_plan_run(db, plan_run.plan_run_id)
    if fresh_pr and fresh_pr.status in _TERMINAL_STATUSES:
        return False, True

    externally_stopped = False
    aborted = False
    stage_runs: list[dict[str, Any]] = []

    for task_ctx in task_batch:
        fresh_pr = _refresh_plan_run(db, plan_run.plan_run_id)
        if fresh_pr and fresh_pr.status in _TERMINAL_STATUSES:
            return aborted, True

        task_id_int = int(task_ctx["task_id"])
        run_params = _attach_plan_run_scope_params(
            _refresh_run_params_with_latest_task_variables(
                db,
                task_id_int,
                task_ctx["run_params"],
            ),
            plan_run.plan_run_id,
        )
        retry_count = int(task_ctx["retry_count"])
        retry_delay = int(task_ctx["retry_delay"])
        on_failure = str(task_ctx["on_failure"])
        item_idx = int(task_ctx["item_idx"])

        try:
            run = run_service.create_run(
                RunCreate(task_id=task_id_int, params=run_params),
                user_id=user_id,
                idempotency_key=(
                    f"plan-{plan_run.plan_run_id}-{task_id_int}"
                    f"-r{round_number}-i{item_idx}-a0"
                ),
            )
        except Exception as exc:
            logger.error(
                "PlanRun %d: create_run failed for task %d in round %d stage %d: %s",
                plan_run.plan_run_id,
                task_id_int,
                round_number,
                stage_number,
                exc,
            )
            failed_items.append(
                {
                    "task_id": task_id_int,
                    "item_idx": item_idx,
                    "on_failure": on_failure,
                    "round": round_number,
                    "stage": stage_number,
                }
            )
            plan_run.status_detail = _merge_plan_run_status_detail(
                plan_run.status_detail,
                _build_launch_status_detail(
                    plan_run_id=plan_run.plan_run_id,
                    launch_waves_total=launch_wave_index,
                    launched_run_ids=launched_run_ids,
                    launch_failed_wave=launch_wave_index,
                    launch_failed_reason=str(exc),
                ),
            )
            db.commit()
            if on_failure == "ABORT":
                aborted = True
            continue

        launched_run_ids.append(run.run_id)
        _persist_run_ids(db, plan_run, launched_run_ids)
        plan_run.status_detail = _merge_plan_run_status_detail(
            plan_run.status_detail,
            _build_launch_status_detail(
                plan_run_id=plan_run.plan_run_id,
                launch_waves_total=launch_wave_index,
                launched_run_ids=launched_run_ids,
            )
            + f";round={round_number},stage={stage_number},"
            f"item={item_idx},run={run.run_id},launch_wave={launch_wave_index} launched",
        )
        db.commit()

        fresh_pr = _refresh_plan_run(db, plan_run.plan_run_id)
        if fresh_pr and fresh_pr.status in _TERMINAL_STATUSES:
            if fresh_pr.status == PlanRunStatus.STOPPED:
                try:
                    run_service.stop_run(
                        run.run_id,
                        reason=f"stopped_from_plan_run:{plan_run.plan_run_id}",
                        user_id=user_id,
                    )
                except Exception:
                    logger.exception(
                        "PlanRun %d: stop pending run %d after terminal guard failed",
                        plan_run.plan_run_id,
                        run.run_id,
                    )
            return aborted, True

        _start_run_execution_in_background(run.run_id)
        stage_runs.append(
            {
                "task_id": task_id_int,
                "run_id": run.run_id,
                "run_params": run_params,
                "retry_count": retry_count,
                "retry_delay": retry_delay,
                "on_failure": on_failure,
                "item_idx": item_idx,
            }
        )

    for stage_run in stage_runs:
        task_id_int = int(stage_run["task_id"])
        run_id = int(stage_run["run_id"])
        retry_count = int(stage_run["retry_count"])
        retry_delay = int(stage_run["retry_delay"])
        on_failure = str(stage_run["on_failure"])
        item_idx = int(stage_run["item_idx"])
        run_params = stage_run["run_params"]

        item_success = False
        for attempt in range(retry_count + 1):
            final_status = _wait_for_run(db, run_id, plan_run.plan_run_id)
            logger.info(
                "PlanRun %d: run %d finished with status %s in round %d stage %d (attempt %d/%d)",
                plan_run.plan_run_id,
                run_id,
                final_status.value,
                round_number,
                stage_number,
                attempt + 1,
                retry_count + 1,
            )

            if final_status == RunStatus.STOPPED:
                fresh_pr = _refresh_plan_run(db, plan_run.plan_run_id)
                if fresh_pr and fresh_pr.status in _TERMINAL_STATUSES:
                    externally_stopped = True
                    break
                terminal_reason = _get_run_terminal_reason(db, run_id)
                if not _is_alert_policy_stop_reason(terminal_reason):
                    externally_stopped = _stop_plan_run_from_child_run(
                        db,
                        plan_run,
                        run_id=run_id,
                        launched_run_ids=launched_run_ids,
                    )
                    break
            if final_status == RunStatus.SUCCEEDED:
                item_success = True
                plan_run.status_detail = _merge_plan_run_status_detail(
                    plan_run.status_detail,
                    f"round={round_number},stage={stage_number},"
                    f"item={item_idx},run={run_id} succeeded",
                )
                db.commit()
                break

            if attempt >= retry_count:
                break

            if retry_delay > 0:
                time.sleep(retry_delay)

            fresh_pr = _refresh_plan_run(db, plan_run.plan_run_id)
            if fresh_pr and fresh_pr.status in _TERMINAL_STATUSES:
                externally_stopped = True
                break

            try:
                retry_run = run_service.create_run(
                    RunCreate(task_id=task_id_int, params=run_params),
                    user_id=user_id,
                    idempotency_key=(
                        f"plan-{plan_run.plan_run_id}-{task_id_int}"
                        f"-r{round_number}-i{item_idx}-a{attempt + 1}"
                    ),
                )
            except Exception as exc:
                logger.error(
                    "PlanRun %d: retry create_run failed for task %d in round %d stage %d: %s",
                    plan_run.plan_run_id,
                    task_id_int,
                    round_number,
                    stage_number,
                    exc,
                )
                break

            run_id = retry_run.run_id
            launched_run_ids.append(run_id)
            _persist_run_ids(db, plan_run, launched_run_ids)
            plan_run.status_detail = _merge_plan_run_status_detail(
                plan_run.status_detail,
                _build_launch_status_detail(
                    plan_run_id=plan_run.plan_run_id,
                    launch_waves_total=launch_wave_index,
                    launched_run_ids=launched_run_ids,
                )
                + f";round={round_number},stage={stage_number},"
                f"item={item_idx},run={run_id},launch_wave={launch_wave_index} retry_launched",
            )
            db.commit()
            _start_run_execution_in_background(run_id)

        if externally_stopped:
            break

        if not item_success:
            failed_items.append(
                {
                    "task_id": task_id_int,
                    "item_idx": item_idx,
                    "on_failure": on_failure,
                    "round": round_number,
                    "stage": stage_number,
                }
            )
            if on_failure == "ABORT":
                aborted = True

    return aborted, externally_stopped


@celery_app.task(bind=True, name="execute_plan_task", max_retries=0)
def execute_plan_task(self, plan_run_id: int, user_id: int) -> dict:
    """
    异步执行 Plan：逐 stage/item 启动 Run，等待结果后按策略处理。

    在 Celery worker 进程中运行，可安全 sleep 轮询。
    """
    db = core_db.SessionLocal()
    try:
        plan_run = db.query(PlanRun).filter(PlanRun.plan_run_id == plan_run_id).first()
        if not plan_run:
            logger.error("PlanRun %d not found", plan_run_id)
            return {"status": "error", "detail": "plan_run_not_found"}

        # ★ 终态守卫：如果已被 stop/fail/succeed，直接返回
        if plan_run.status in _TERMINAL_STATUSES:
            logger.info(
                "PlanRun %d already in terminal state %s, skipping",
                plan_run_id,
                plan_run.status.value,
            )
            return {"status": plan_run.status.value, "detail": "already_terminal"}

        plan = db.query(Plan).filter(Plan.plan_id == plan_run.plan_id).first()
        if not plan:
            _fail_plan_run(db, plan_run, "plan_not_found")
            return {"status": "error", "detail": "plan_not_found"}

        # ★ Plan.status → RUNNING（不污染模板更新时间）
        _set_plan_status_without_touching_updated_at(db, plan, PlanStatus.RUNNING)
        db.commit()

        # --- 进入 RUNNING ---
        plan_run.status = PlanRunStatus.RUNNING
        plan_run.status_detail = _merge_plan_run_status_detail(
            plan_run.status_detail,
            _build_launch_status_detail(
                plan_run_id=plan_run.plan_run_id,
                launch_waves_total=0,
                launched_run_ids=[],
            )
            + ";executing_stages",
        )
        db.commit()

        stages = plan.stages if isinstance(plan.stages, list) else []
        if not stages:
            _fail_plan_run(db, plan_run, "no_stages")
            _recover_plan_status(db, plan)
            return {"status": "failed", "detail": "no_stages"}

        from app.services.run_service import RunService
        from common.schemas.run import RunCreate

        run_service = RunService(db)
        launched_run_ids: list[int] = []
        failed_items: list[dict] = []
        aborted = False
        externally_stopped = False
        item_idx = 0
        total_rounds = _resolve_total_rounds(plan)
        launch_waves_total = 0

        def _stage_key(raw: dict[str, Any]) -> int:
            try:
                return int(raw.get("stage", 0))
            except (TypeError, ValueError):
                return 0

        for round_number in range(1, total_rounds + 1):
            if aborted or externally_stopped:
                break

            round_aborted = False
            plan_run.round = round_number
            plan_run.status_detail = _merge_plan_run_status_detail(
                plan_run.status_detail, f"round={round_number};executing_stages"
            )
            db.commit()

            for stage in sorted(stages, key=_stage_key):
                if aborted or externally_stopped or round_aborted:
                    break
                items = stage.get("items") if isinstance(stage, dict) else None
                if not isinstance(items, list):
                    continue

                task_batch: list[dict[str, Any]] = []

                for item in items:
                    if aborted or externally_stopped or round_aborted:
                        break

                    if not isinstance(item, dict):
                        continue
                    item = _resolve_stage_item_for_round(item, round_number)
                    item_type = item.get("type")
                    if item_type == PlanStageItemType.POSTPROCESSOR.value:
                        if task_batch:
                            launch_waves_total += 1
                            stage_aborted, stage_stopped = _execute_stage_task_batch(
                                db=db,
                                plan_run=plan_run,
                                run_service=run_service,
                                user_id=user_id,
                                round_number=round_number,
                                stage_number=_stage_key(stage),
                                task_batch=task_batch,
                                launch_wave_index=launch_waves_total,
                                launched_run_ids=launched_run_ids,
                                failed_items=failed_items,
                            )
                            task_batch = []
                            if stage_aborted:
                                if round_number < total_rounds:
                                    round_aborted = True
                                else:
                                    aborted = True
                            externally_stopped = externally_stopped or stage_stopped
                            if aborted or externally_stopped or round_aborted:
                                break

                        postprocessor_sleep_seconds = (
                            _resolve_postprocessor_sleep_seconds(item)
                        )
                        postprocessor_action = (
                            "sleep" if postprocessor_sleep_seconds > 0 else "noop"
                        )
                        next_item_idx = item_idx + 1
                        plan_run.status_detail = _merge_plan_run_status_detail(
                            plan_run.status_detail,
                            f"round={round_number},stage={_stage_key(stage)},"
                            f"item={next_item_idx},postprocessor={postprocessor_action};"
                            + _build_postprocessor_status_detail(
                                round_number=round_number,
                                stage_number=_stage_key(stage),
                                item_idx=next_item_idx,
                                action=postprocessor_action,
                                seconds=postprocessor_sleep_seconds,
                            ),
                        )
                        db.commit()
                        if postprocessor_sleep_seconds > 0:
                            plan_run.status_detail = _merge_plan_run_status_detail(
                                plan_run.status_detail,
                                _build_postprocessor_status_detail(
                                    round_number=round_number,
                                    stage_number=_stage_key(stage),
                                    item_idx=next_item_idx,
                                    action="sleep_started",
                                    seconds=postprocessor_sleep_seconds,
                                ),
                            )
                            db.commit()
                            if _run_postprocessor(plan_run, item):
                                plan_run.status_detail = _merge_plan_run_status_detail(
                                    plan_run.status_detail,
                                    _build_postprocessor_status_detail(
                                        round_number=round_number,
                                        stage_number=_stage_key(stage),
                                        item_idx=next_item_idx,
                                        action="sleep_completed",
                                        seconds=postprocessor_sleep_seconds,
                                    ),
                                )
                                db.commit()
                            else:
                                plan_run.status_detail = _merge_plan_run_status_detail(
                                    plan_run.status_detail,
                                    _build_postprocessor_status_detail(
                                        round_number=round_number,
                                        stage_number=_stage_key(stage),
                                        item_idx=next_item_idx,
                                        action="sleep_interrupted",
                                        seconds=postprocessor_sleep_seconds,
                                    ),
                                )
                                db.commit()
                                externally_stopped = True
                        item_idx += 1
                        continue
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
                    if not isinstance(run_params, dict):
                        run_params = None

                    retry_count = max(0, int(item.get("retry_count", 0) or 0))
                    retry_delay = max(0, int(item.get("retry_delay_seconds", 10) or 10))
                    on_failure = str(item.get("on_failure", "ABORT")).upper()
                    item_idx += 1
                    task_batch.append(
                        {
                            "task_id": task_id_int,
                            "run_params": run_params,
                            "retry_count": retry_count,
                            "retry_delay": retry_delay,
                            "on_failure": on_failure,
                            "item_idx": item_idx,
                        }
                    )

                if (
                    task_batch
                    and not aborted
                    and not externally_stopped
                    and not round_aborted
                ):
                    launch_waves_total += 1
                    stage_aborted, stage_stopped = _execute_stage_task_batch(
                        db=db,
                        plan_run=plan_run,
                        run_service=run_service,
                        user_id=user_id,
                        round_number=round_number,
                        stage_number=_stage_key(stage),
                        task_batch=task_batch,
                        launch_wave_index=launch_waves_total,
                        launched_run_ids=launched_run_ids,
                        failed_items=failed_items,
                    )
                    if stage_aborted:
                        if round_number < total_rounds:
                            round_aborted = True
                        else:
                            aborted = True
                    externally_stopped = externally_stopped or stage_stopped

        fresh_terminal_plan_run = _refresh_plan_run(db, plan_run_id)
        if (
            fresh_terminal_plan_run
            and fresh_terminal_plan_run.status in _TERMINAL_STATUSES
        ):
            plan_run = fresh_terminal_plan_run
            externally_stopped = fresh_terminal_plan_run.status == PlanRunStatus.STOPPED

        # --- 终态收敛 ---
        if externally_stopped:
            # 外部 stop 已处理，不覆盖状态
            logger.info(
                "PlanRun %d externally stopped, skipping final state update",
                plan_run_id,
            )
        elif aborted:
            _fail_plan_run(
                db,
                plan_run,
                f"aborted_on_failure;failed={len(failed_items)}",
            )
        elif not launched_run_ids:
            _fail_plan_run(db, plan_run, "launch_failed")
        elif failed_items:
            _fail_plan_run(
                db,
                plan_run,
                f"partial_failure;failed={len(failed_items)}",
            )
        elif not _has_completed_all_expected_runs(
            stages=stages,
            total_rounds=total_rounds,
            launched_run_ids=launched_run_ids,
        ):
            _fail_plan_run(
                db,
                plan_run,
                "incomplete_plan_execution",
            )
        else:
            # 全部成功
            plan_run.status = PlanRunStatus.SUCCEEDED
            plan_run.status_detail = _merge_plan_run_status_detail(
                plan_run.status_detail, "all_succeeded"
            )
            plan_run.ended_at = _resolve_plan_run_end_ts(
                db,
                launched_run_ids,
                _utcnow(),
            )
            duration_seconds = _compute_plan_run_duration_seconds(
                getattr(plan_run, "started_at", None),
                plan_run.ended_at,
            )
            if duration_seconds is not None:
                plan_run.duration_seconds = duration_seconds
            db.commit()

        # ★ 回收 Plan.status
        if plan:
            _recover_plan_status(db, plan)

        if plan_run.status in _TERMINAL_STATUSES:
            _trigger_plan_run_terminal_webhooks(db, plan_run)
            _auto_generate_mixed_run_report_if_needed(db, plan_run)

        result_dict = {
            "status": plan_run.status.value,
            "launched": len(launched_run_ids),
            "failed": len(failed_items),
            "execution_session_id": _build_execution_session_id(plan_run_id),
            "launch_waves_total": launch_waves_total,
            "launched_run_ids": list(launched_run_ids),
        }
        logger.info(
            "PlanRun %d: execute_plan_task finished successfully with result %s",
            plan_run_id,
            result_dict,
        )
        return result_dict
    except Exception as exc:
        logger.error("execute_plan_task error for PlanRun %d: %s", plan_run_id, exc)
        try:
            pr = db.query(PlanRun).filter(PlanRun.plan_run_id == plan_run_id).first()
            if pr and pr.status not in _TERMINAL_STATUSES:
                _fail_plan_run(db, pr, f"unexpected_error:{exc}")
            p = (
                db.query(Plan).filter(Plan.plan_id == pr.plan_id).first()
                if pr
                else None
            )
            if p:
                _recover_plan_status(db, p)
        except Exception:
            pass
        raise
    finally:
        db.close()


def _fail_plan_run(db, plan_run: PlanRun, detail: str) -> None:
    fresh_plan_run = _refresh_plan_run(db, plan_run.plan_run_id)
    if fresh_plan_run and fresh_plan_run.status in _TERMINAL_STATUSES:
        logger.info(
            "PlanRun %d already in terminal state %s, skipping fail override",
            plan_run.plan_run_id,
            fresh_plan_run.status.value,
        )
        return
    plan_run.status = PlanRunStatus.FAILED
    plan_run.status_detail = _merge_plan_run_status_detail(
        plan_run.status_detail, detail
    )
    plan_run.ended_at = _utcnow()
    duration_seconds = _compute_plan_run_duration_seconds(
        getattr(plan_run, "started_at", None),
        plan_run.ended_at,
    )
    if duration_seconds is not None:
        plan_run.duration_seconds = duration_seconds
    db.commit()
    _auto_generate_mixed_run_report_if_needed(db, plan_run)


def _recover_plan_status(db, plan: Plan) -> None:
    """Plan 无活跃 PlanRun 时回收状态。"""
    active = (
        db.query(PlanRun)
        .filter(
            PlanRun.plan_id == plan.plan_id,
            PlanRun.status.in_([PlanRunStatus.PREPARING, PlanRunStatus.RUNNING]),
        )
        .count()
    )
    if active == 0 and plan.status == PlanStatus.RUNNING:
        _set_plan_status_without_touching_updated_at(db, plan, PlanStatus.READY)
        db.commit()


@celery_app.task(
    bind=True,
    name="execute_plan_run_k6_control_task",
    max_retries=0,
    track_started=True,
)
def execute_plan_run_k6_control_task(
    self,
    plan_run_id: int,
    request_payload: dict[str, Any],
    user_id: int | None = None,
) -> dict[str, Any]:
    db = core_db.SessionLocal()
    try:
        from app.services.plan_service import PlanService

        service = PlanService(db)
        request = PlanRunK6BroadcastRequest(**request_payload)
        request.async_mode = False
        result = service.broadcast_plan_run_k6_control(
            plan_run_id=plan_run_id,
            request=request,
            user_id=user_id,
            followup_scenario_direct_false_reject=True,
        )
        return result.model_dump(mode="json")
    finally:
        db.close()


def build_plan_run_k6_control_task_status(
    *,
    plan_run_id: int,
    selected_round: int,
    task_id: str,
) -> PlanRunK6BroadcastTaskStatusResponse:
    task_result = AsyncResult(task_id, app=celery_app)
    state = str(task_result.state or "PENDING").strip().lower()
    completed = task_result.ready()
    result_payload = None
    error = None

    if completed:
        if task_result.successful():
            raw_result = task_result.result
            if isinstance(raw_result, PlanRunK6BroadcastResponse):
                result_payload = raw_result
            elif isinstance(raw_result, dict):
                result_payload = PlanRunK6BroadcastResponse.model_validate(raw_result)
        else:
            error = str(task_result.result)

    return PlanRunK6BroadcastTaskStatusResponse(
        plan_run_id=plan_run_id,
        selected_round=selected_round,
        async_task_id=task_id,
        job_status=state,
        completed=completed,
        result=result_payload,
        error=error,
    )
