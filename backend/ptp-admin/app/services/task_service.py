from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, List, Optional, Tuple
from uuid import uuid4

from app.core.nacos_client import get_nacos_client
from app.repositories.run_repository import RunRepository
from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.orm import Session

from app.models.report import Report
from app.models.run import Run
from app.models.script import Script
from app.models.task import Task
from app.models.task_asset import TaskAsset
from app.models.task_version import TaskVersionRecord
from app.models.user import User, UserRole
from app.services.run_service import RunService
from app.services.task_asset_service import TaskAssetService
from app.services.scenario_quality_lint_service import ScenarioQualityLintService
from common.config.settings import settings
from common.models.enums import EngineType, RunStatus, ScriptType, TaskPattern, TaskStatus
from common.utils import s3_utils
from app.repositories.task_repository import TaskRepository
from app.repositories.task_asset_repository import TaskAssetRepository
from app.schemas.task import (
    TaskBatchStopResponse,
    TaskCreate,
    TaskLastRunParamsResponse,
    TaskPrepareRunResponse,
    TaskPodCapacityItem,
    TaskResourcePoolSummary,
    TaskScriptCompareResponse,
    TaskScriptVersionContent,
    TaskSummaryResponse,
    TaskVersionDetailResponse,
    TaskUpdate,
    TaskVersionRecordResponse,
)

import logging

logger = logging.getLogger(__name__)

DEFAULT_TASK_DURATION_SECONDS = 300
MANAGED_RUN_CONTROL_PARAM_KEYS = {
    "pod_count",
    "pod_num",
    "pod_total",
    "thread_count",
    "num_threads",
    "threads",
    "vus",
    "duration",
    "duration_seconds",
    "iterations",
    "request_count",
    "loops",
    "run_mode",
    "run_by",
    "total_requests",
}

RUN_STATUS_LABELS: dict[str, str] = {
    RunStatus.PREPARING.value: "准备中",
    RunStatus.RUNNING.value: "运行中",
    RunStatus.SUCCEEDED.value: "成功",
    RunStatus.FAILED.value: "失败",
    RunStatus.STOPPED.value: "已停止",
}

ENGINE_SCRIPT_TYPE_MAP: dict[EngineType, ScriptType] = {
    EngineType.JMETER: ScriptType.JMETER,
    EngineType.K6: ScriptType.K6,
}


def _task_scripts_dir() -> Path:
    root_dir = Path(__file__).resolve().parent.parent.parent.parent.parent
    scripts_dir = root_dir / "tmp_scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    return scripts_dir


class TaskService:
    def __init__(self, db: Session):
        self.db = db
        self.repo = TaskRepository(db)
        self.run_repo = RunRepository(db)
        self.task_asset_repo = TaskAssetRepository(db)

    def create_task(self, task_in: TaskCreate, user_id: int) -> Task:
        task_data = task_in.model_dump()
        task_data["duration"] = self._resolve_task_duration_seconds(task_data)
        task_data["protocols"] = [p.value for p in task_in.protocols]
        task_data["created_by"] = user_id
        task_data["status"] = TaskStatus.DRAFT
        self._validate_engine_script_match(
            task_in.engine_type,
            task_data.get("script_id"),
        )
        cloned_script_id = self._clone_script_for_task(
            task_data.get("script_id"), user_id
        )
        if cloned_script_id is not None:
            task_data["script_id"] = cloned_script_id
        task = Task(**task_data)
        created = self.repo.create(task)
        created_id = created.id
        self._create_task_version_snapshot(created, user_id=user_id)
        reloaded = self.repo.find_by_id(created_id)
        if reloaded is None:
            return created
        self._attach_task_display_fields([reloaded])
        return reloaded

    def list_tasks(
        self,
        status: Optional[TaskStatus] = None,
        last_run_status: Optional[RunStatus] = None,
        task_id: Optional[int] = None,
        name: Optional[str] = None,
        protocol: Optional[str] = None,
        engine_type: Optional[str] = None,
        env: Optional[str] = None,
        script_id: Optional[int] = None,
        task_pattern: Optional[str] = None,
        created_by: Optional[int] = None,
        created_by_name: Optional[str] = None,
        participant_id: Optional[int] = None,
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
        business_line: Optional[str] = None,
        app_name: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[Task], int]:
        skip = (page - 1) * page_size
        if last_run_status is not None or created_by_name:
            items, _total = self.repo.find_all(
                status=status,
                task_id=task_id,
                name=name,
                protocol=protocol,
                engine_type=engine_type,
                env=env,
                script_id=script_id,
                task_pattern=task_pattern,
                created_by=created_by,
                participant_id=participant_id,
                created_from=created_from,
                created_to=created_to,
                business_line=business_line,
                app_name=app_name,
                skip=0,
                limit=None,
            )
            self._attach_task_display_fields(items)
            filtered_items = items
            if last_run_status is not None:
                normalized_last_run_status = (
                    str(getattr(last_run_status, "value", last_run_status) or "")
                    .strip()
                    .lower()
                )
                filtered_items = [
                    task
                    for task in filtered_items
                    if str(getattr(task, "last_run_status", "") or "").strip().lower()
                    == normalized_last_run_status
                ]
            if created_by_name:
                normalized_created_by_name = created_by_name.strip().casefold()
                filtered_items = [
                    task
                    for task in filtered_items
                    if normalized_created_by_name
                    in str(getattr(task, "created_by_name", "") or "")
                    .strip()
                    .casefold()
                ]
            total = len(filtered_items)
            return filtered_items[skip : skip + page_size], total

        items, total = self.repo.find_all(
            status=status,
            task_id=task_id,
            name=name,
            protocol=protocol,
            engine_type=engine_type,
            env=env,
            script_id=script_id,
            task_pattern=task_pattern,
            created_by=created_by,
            participant_id=participant_id,
            created_from=created_from,
            created_to=created_to,
            business_line=business_line,
            app_name=app_name,
            skip=skip,
            limit=page_size,
        )
        self._attach_task_display_fields(items)
        return items, total

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

    @staticmethod
    def _parse_duration_seconds(value: Any) -> Optional[int]:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            parsed = int(float(value))
            return parsed if parsed > 0 else None
        if not isinstance(value, str):
            return None
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = int(float(raw))
        except ValueError:
            parsed = None
        if parsed is not None:
            return parsed if parsed > 0 else None

        match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)", raw, re.IGNORECASE)
        if not match:
            return None
        amount = float(match.group(1))
        unit = match.group(2).lower()
        if amount <= 0:
            return None
        if unit == "ms":
            return max(1, int(amount / 1000.0 + 0.999999))
        if unit == "s":
            return max(1, int(amount + 0.999999))
        if unit == "m":
            return max(1, int(amount * 60 + 0.999999))
        if unit == "h":
            return max(1, int(amount * 3600 + 0.999999))
        return None

    def _resolve_task_duration_seconds(self, task_data: dict[str, Any]) -> int:
        direct_duration = self._parse_duration_seconds(task_data.get("duration"))
        if direct_duration:
            return direct_duration

        properties = (
            task_data.get("properties")
            if isinstance(task_data.get("properties"), dict)
            else {}
        )
        variables = (
            properties.get("variables")
            if isinstance(properties.get("variables"), dict)
            else {}
        )
        variable_duration = self._parse_duration_seconds(variables.get("duration"))
        if variable_duration:
            return variable_duration

        return DEFAULT_TASK_DURATION_SECONDS

    def get_task_summary(
        self,
        env: Optional[str] = None,
        created_by: Optional[int] = None,
        participant_id: Optional[int] = None,
    ) -> TaskSummaryResponse:
        task_scope_query = self._build_task_summary_scope_query(
            env=env,
            created_by=created_by,
            participant_id=participant_id,
        )
        if task_scope_query is None:
            counts = self._get_task_summary_counts_with_python_participant(
                env=env,
                created_by=created_by,
                participant_id=participant_id,
            )
        else:
            counts = self._get_task_summary_counts(task_scope_query.subquery())

        if counts["task_total"] == 0:
            return TaskSummaryResponse(
                task_total=0,
                task_success_total=0,
                task_failed_total=0,
                report_total=0,
                pod_capacity=self._build_pod_capacity(env),
                resource_pool=self._build_resource_pool(env),
            )

        return TaskSummaryResponse(
            task_total=counts["task_total"],
            task_success_total=counts["task_success_total"],
            task_failed_total=counts["task_failed_total"],
            report_total=counts["report_total"],
            pod_capacity=self._build_pod_capacity(env),
            resource_pool=self._build_resource_pool(env),
        )

    def _build_task_summary_scope_query(
        self,
        env: Optional[str],
        created_by: Optional[int],
        participant_id: Optional[int],
    ):
        query = self.db.query(Task.id)
        expected_envs = self._normalize_multi_value_filter(env) if env else set()
        if expected_envs:
            query = query.filter(func.lower(Task.env).in_(expected_envs))
        if created_by is not None:
            query = query.filter(Task.created_by == created_by)
        if participant_id is not None:
            collaborator_condition = self._collaborator_participant_condition(
                participant_id
            )
            if collaborator_condition is None:
                return None
            query = query.filter(
                or_(Task.created_by == participant_id, collaborator_condition)
            )
        return query

    def _collaborator_participant_condition(self, participant_id: int):
        dialect_name = ""
        if self.db.bind is not None:
            dialect_name = self.db.bind.dialect.name

        if dialect_name == "sqlite":
            collaborators = (
                func.json_each(Task.collaborator_ids)
                .table_valued("value")
                .alias("collaborator")
            )
            return exists(
                select(1)
                .select_from(collaborators)
                .where(cast(collaborators.c.value, String) == str(participant_id))
            )

        if dialect_name in {"mysql", "mariadb"}:
            return or_(
                func.JSON_CONTAINS(
                    Task.collaborator_ids, json.dumps(int(participant_id))
                )
                == 1,
                func.JSON_CONTAINS(
                    Task.collaborator_ids, json.dumps(str(participant_id))
                )
                == 1,
            )

        return None

    def _get_task_summary_counts(self, task_scope_subquery) -> dict[str, int]:
        task_total = (
            self.db.query(func.count()).select_from(task_scope_subquery).scalar() or 0
        )
        if task_total == 0:
            return {
                "task_total": 0,
                "task_success_total": 0,
                "task_failed_total": 0,
                "report_total": 0,
            }

        run_counts = (
            self.db.query(Run.run_status, func.count(Run.run_id))
            .join(task_scope_subquery, Run.task_id == task_scope_subquery.c.id)
            .filter(Run.run_status.in_([RunStatus.SUCCEEDED, RunStatus.FAILED]))
            .group_by(Run.run_status)
            .all()
        )
        run_count_by_status = {
            str(getattr(status, "value", status)): int(count)
            for status, count in run_counts
        }
        report_total = (
            self.db.query(func.count(Report.id))
            .join(task_scope_subquery, Report.task_id == task_scope_subquery.c.id)
            .scalar()
            or 0
        )
        return {
            "task_total": int(task_total),
            "task_success_total": run_count_by_status.get(RunStatus.SUCCEEDED.value, 0),
            "task_failed_total": run_count_by_status.get(RunStatus.FAILED.value, 0),
            "report_total": int(report_total),
        }

    def _get_task_summary_counts_with_python_participant(
        self,
        env: Optional[str],
        created_by: Optional[int],
        participant_id: Optional[int],
    ) -> dict[str, int]:
        query = self.db.query(Task.id, Task.created_by, Task.collaborator_ids)
        expected_envs = self._normalize_multi_value_filter(env) if env else set()
        if expected_envs:
            query = query.filter(func.lower(Task.env).in_(expected_envs))
        if created_by is not None:
            query = query.filter(Task.created_by == created_by)

        task_ids = [
            row.id
            for row in query.all()
            if participant_id is None
            or self._row_matches_participant(
                row.created_by, row.collaborator_ids, participant_id
            )
        ]
        if not task_ids:
            return {
                "task_total": 0,
                "task_success_total": 0,
                "task_failed_total": 0,
                "report_total": 0,
            }
        task_scope_subquery = (
            self.db.query(Task.id).filter(Task.id.in_(task_ids)).subquery()
        )
        return self._get_task_summary_counts(task_scope_subquery)

    @staticmethod
    def _row_matches_participant(
        created_by: Optional[int],
        collaborator_ids: Any,
        participant_id: int,
    ) -> bool:
        if created_by is not None and int(created_by) == int(participant_id):
            return True
        collaborators = collaborator_ids if isinstance(collaborator_ids, list) else []
        for collaborator in collaborators:
            try:
                if int(collaborator) == int(participant_id):
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def get_task(self, task_id: int) -> Optional[Task]:
        task = self.repo.find_by_id(task_id)
        if task:
            self._attach_task_display_fields([task])
        return task

    def get_last_run_params(
        self, task_id: int, user_id: Optional[int] = None
    ) -> Optional[TaskLastRunParamsResponse]:
        task = self.repo.find_by_id(task_id)
        if not task:
            return None

        self._ensure_task_owner(task, user_id)

        default_run_params = self._build_default_run_params(task)
        variable_keys = {
            key
            for key in default_run_params.keys()
            if self._is_editable_run_param_key(key)
        }
        latest_run = self.run_repo.find_latest_by_task_id(task_id)
        raw_last_run_params = (
            self._sanitize_run_params(latest_run.params) if latest_run else None
        )
        last_run_params = self._filter_editable_last_run_params(
            raw_last_run_params,
            variable_keys,
        )

        return TaskLastRunParamsResponse(
            task_pattern=task.task_pattern,
            default_run_params=default_run_params or None,
            last_run_params=last_run_params or None,
            param_source="last_run_preferred" if last_run_params else "default_only",
            last_run_id=latest_run.run_id if latest_run else None,
            last_run_status=latest_run.run_status.value if latest_run else None,
            last_run_started_at=latest_run.started_at if latest_run else None,
            param_count=self._editable_run_param_count(
                last_run_params or default_run_params
            ),
        )

    def update_task(
        self, task_id: int, task_in: TaskUpdate, user_id: Optional[int] = None
    ) -> Optional[Task]:
        task = self.repo.find_by_id(task_id)
        if not task:
            return None
        self._ensure_task_owner(task, user_id)
        updates = task_in.model_dump(exclude_unset=True)
        if "protocols" in updates and updates["protocols"] is not None:
            updates["protocols"] = [p.value for p in updates["protocols"]]
        if updates.get("engine_type") is None:
            updates.pop("engine_type", None)
        if updates.get("script_id") is None:
            updates.pop("script_id", None)
        effective_engine_type = updates.get("engine_type", task.engine_type)
        effective_script_id = updates.get("script_id", task.script_id)
        self._validate_engine_script_match(effective_engine_type, effective_script_id)
        requested_script_id = updates.get("script_id")
        if requested_script_id is not None and int(requested_script_id) != int(
            task.script_id
        ):
            cloned_script_id = self._clone_script_for_task(requested_script_id, user_id)
            if cloned_script_id is not None:
                updates["script_id"] = cloned_script_id
        updated = self.repo.update(task_id, updates)
        if updated:
            self._create_task_version_snapshot(updated, user_id=user_id)
            self._attach_task_display_fields([updated])
        return updated

    def prepare_task_for_run(
        self,
        task_id: int,
        user_id: Optional[int] = None,
    ) -> TaskPrepareRunResponse:
        task = self.repo.find_by_id(task_id)
        if not task:
            raise ValueError("Task not found")
        self._ensure_task_owner(task, user_id)
        self._validate_prepare_run_status(task)
        latest_run = self._build_latest_run_map([int(task.id)]).get(int(task.id))
        if latest_run and latest_run.run_status in {
            RunStatus.PREPARING,
            RunStatus.RUNNING,
        }:
            raise ValueError("Task already has active run")

        if not self._get_script(task.script_id):
            updated = self.repo.update_status(task.id, TaskStatus.SCRIPT_MISSING)
            if updated is None:
                raise ValueError("Task not found")
            task = updated
            return TaskPrepareRunResponse(
                task=task,
                next_action="upload_script",
                status_hint="任务缺少可用脚本，请先补齐脚本后再启动压测。",
            )

        next_status = TaskStatus.READY
        if task.status != next_status:
            updated = self.repo.update_status(task.id, next_status)
            if updated is None:
                raise ValueError("Task not found")
            task = updated

        return TaskPrepareRunResponse(
            task=task,
            next_action="start_run",
            status_hint="任务已准备完成，可以直接启动压测。",
        )

    def copy_task(self, task_id: int, user_id: int) -> Task:
        task = self.repo.find_by_id(task_id)
        if not task:
            raise ValueError("Task not found")
        self._ensure_task_owner(task, user_id)
        cloned_script_id = self._clone_script_for_task(task.script_id, user_id)

        copied = Task(
            name=f"{task.name}-copy",
            description=task.description,
            env=task.env,
            script_id=cloned_script_id or task.script_id,
            status=TaskStatus.DRAFT,
            engine_type=task.engine_type,
            task_pattern=task.task_pattern,
            protocols=deepcopy(task.protocols),
            thread_count=task.thread_count,
            duration=task.duration,
            ramp_up=task.ramp_up,
            properties=deepcopy(task.properties),
            collaborator_ids=deepcopy(task.collaborator_ids),
            created_by=user_id,
            last_run_at=None,
        )
        created = self.repo.create(copied)
        source_assets = self.task_asset_repo.find_all(task_id=task.id)
        asset_service = TaskAssetService(self.db)
        for asset in source_assets:
            asset_service.clone_asset_to_task(
                asset=asset,
                task_id=created.id,
                user_id=user_id,
            )
        self._create_task_version_snapshot(created, user_id=user_id)
        reloaded = self.repo.find_by_id(created.id)
        if reloaded is None:
            return created
        self._attach_task_display_fields([reloaded])
        return reloaded

    def delete_task(self, task_id: int, user_id: Optional[int] = None) -> None:
        task = self.repo.find_by_id(task_id)
        if not task:
            raise ValueError("Task not found")
        self._ensure_task_owner(task, user_id)
        if task.status == TaskStatus.RUNNING:
            raise ValueError("Cannot delete running task")
        self.repo.delete(task_id)

    def batch_stop_tasks(
        self,
        task_ids: list[int],
        reason: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> TaskBatchStopResponse:
        normalized_task_ids = sorted(
            {int(task_id) for task_id in task_ids if int(task_id) > 0}
        )
        run_service = RunService(self.db)
        stopped_task_ids: list[int] = []
        stopped_run_ids: list[int] = []
        skipped_task_ids: list[int] = []

        for task_id in normalized_task_ids:
            task = self.repo.find_by_id(task_id)
            if not task:
                skipped_task_ids.append(task_id)
                continue
            self._ensure_task_owner(task, user_id)
            running_runs = (
                self.db.query(Run)
                .filter(Run.task_id == task_id)
                .filter(Run.run_status == RunStatus.RUNNING)
                .all()
            )
            if not running_runs:
                skipped_task_ids.append(task_id)
                continue
            stopped_task_ids.append(task_id)
            for run in running_runs:
                stopped = run_service.stop_run(
                    run.run_id,
                    reason=reason or "batch_stop_from_tasks",
                    user_id=user_id,
                )
                if stopped:
                    stopped_run_ids.append(stopped.run_id)

        return TaskBatchStopResponse(
            requested_task_ids=normalized_task_ids,
            stopped_task_ids=stopped_task_ids,
            stopped_run_ids=stopped_run_ids,
            skipped_task_ids=skipped_task_ids,
        )

    def list_task_versions(
        self,
        task_id: int,
        page: int = 1,
        page_size: int = 20,
        user_id: Optional[int] = None,
    ) -> tuple[list[TaskVersionRecordResponse], int]:
        task = self.repo.find_by_id(task_id)
        if not task:
            raise ValueError("Task not found")
        self._ensure_task_owner(task, user_id)

        query = (
            self.db.query(TaskVersionRecord)
            .filter(TaskVersionRecord.task_id == task_id)
            .order_by(TaskVersionRecord.id.desc())
        )
        total = query.count()
        items = query.offset((page - 1) * page_size).limit(page_size).all()
        return self._build_task_version_responses(items), total

    def compare_task_script(
        self, task_id: int, history_version: str, user_id: Optional[int] = None
    ) -> TaskScriptCompareResponse:
        task = self.repo.find_by_id(task_id)
        if not task:
            raise ValueError("Task not found")
        self._ensure_task_owner(task, user_id)
        self._attach_task_display_fields([task])

        history = (
            self.db.query(TaskVersionRecord)
            .filter(TaskVersionRecord.task_id == task_id)
            .filter(TaskVersionRecord.version == history_version)
            .first()
        )
        if not history:
            raise ValueError("Task history version not found")

        current_version = self._resolve_latest_version(task_id)
        current_script = self._build_script_version_content(
            version=current_version,
            script=self._get_script(task.script_id),
            content=self._load_script_content(task.script_id),
            created_by_name=task.created_by_name,
            updated_at=task.updated_at or task.created_at,
        )

        history_snapshot = self._load_task_snapshot(history)
        history_file_name = history_snapshot.get("script_name")
        history_created_by_name = self._resolve_task_version_created_by_name(history)
        history_script = TaskScriptVersionContent(
            version=history.version,
            content=history.script_content or "",
            file_name=history_file_name if isinstance(history_file_name, str) else None,
            created_by_name=history_created_by_name,
            updated_at=history.updated_at or history.created_at,
        )
        return TaskScriptCompareResponse(current=current_script, history=history_script)

    def get_task_version_detail(
        self, task_id: int, version: str, user_id: Optional[int] = None
    ) -> TaskVersionDetailResponse:
        task = self.repo.find_by_id(task_id)
        if not task:
            raise ValueError("Task not found")
        self._ensure_task_owner(task, user_id)

        history = (
            self.db.query(TaskVersionRecord)
            .filter(TaskVersionRecord.task_id == task_id)
            .filter(TaskVersionRecord.version == version)
            .first()
        )
        if not history:
            raise ValueError("Task history version not found")

        return TaskVersionDetailResponse(
            version=history.version,
            created_by_name=self._resolve_task_version_created_by_name(history),
            created_at=history.created_at,
            updated_at=history.updated_at,
            snapshot=self._load_task_snapshot(history),
        )

    def _ensure_task_owner(self, task: Task, user_id: Optional[int]) -> None:
        if user_id is None:
            return
        if self._is_task_access_exempt_user(user_id):
            return
        if task.created_by and int(task.created_by) != int(user_id):
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

    def _validate_prepare_run_status(self, task: Task) -> None:
        if task.status == TaskStatus.RUNNING:
            raise ValueError("Running task cannot be prepared again")

    def _build_pod_capacity(self, env: Optional[str]) -> list[TaskPodCapacityItem]:
        env_value = (env or "").lower()
        if env_value in {"prod", "mainnet", "production"}:
            rows = [
                ("aws-mainnet", "AWS主网压测集群", 0, "8C18G", False),
                ("tx-mainnet", "TX主网压测集群", 0, "8C18G", False),
                ("ec2", "EC2", 0, "8C18G", True),
            ]
        else:
            rows = [
                ("aws-test", "AWS测试压测集群", 0, "8C18G", False),
                ("tx-test", "TX测试压测集群", 0, "8C18G", False),
                ("ec2", "EC2", 0, "8C18G", True),
                ("aws-mainnet", "AWS主网压测集群", 0, "8C18G", False),
                ("tx-mainnet", "TX主网压测集群", 0, "8C18G", False),
            ]
        return [
            TaskPodCapacityItem(
                cluster_code=cluster_code,
                cluster_name=cluster_name,
                available_pod_count=available_pod_count,
                resource_spec=resource_spec,
                readonly=readonly,
            )
            for cluster_code, cluster_name, available_pod_count, resource_spec, readonly in rows
        ]

    def _build_resource_pool(self, env: Optional[str]) -> TaskResourcePoolSummary:
        pod_capacity = self._build_pod_capacity(env)
        resource_spec = next(
            (item.resource_spec for item in pod_capacity if item.resource_spec),
            "8C18G",
        )
        standby_total = len(self._discover_agent_hosts())
        in_use_total = self._estimate_in_use_agents(env)
        in_use_total = min(in_use_total, standby_total)
        idle_total = max(standby_total - in_use_total, 0)
        return TaskResourcePoolSummary(
            standby_total=standby_total,
            in_use_total=in_use_total,
            idle_total=idle_total,
            engine_compatibility=["jmeter", "k6"],
            standby_total_source="ptp-agent discovery (Nacos first, static fallback when discovery empty or unavailable)",
            resource_spec=resource_spec,
        )

    def _build_agent_capacity_context(self, env: Optional[str]) -> dict[str, Any]:
        hosts = self._discover_agent_hosts()
        in_use_by_host = self._estimate_in_use_agents_by_host(env)
        feedback_by_host = self._build_agent_scheduling_feedback_by_host(env, hosts)
        return {
            "hosts": {
                host: {
                    "in_use": in_use_by_host.get(host, 0),
                    "healthy": True,
                    **feedback_by_host.get(host, {}),
                }
                for host in hosts
            },
            "in_use_by_host": in_use_by_host,
            "feedback_by_host": feedback_by_host,
            "feedback_penalty_by_host": {
                host: item["feedback_penalty"]
                for host, item in feedback_by_host.items()
                if "feedback_penalty" in item
            },
        }

    def _build_agent_scheduling_feedback_by_host(
        self, env: Optional[str], hosts: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Summarize recent terminal run outcomes as a light scheduling tie-breaker."""

        if not hosts:
            return {}
        lookback_seconds = self._agent_scheduling_feedback_lookback_seconds()
        if lookback_seconds <= 0:
            return {}

        since = datetime.now(timezone.utc) - timedelta(seconds=lookback_seconds)
        query = (
            self.db.query(Run)
            .filter(Run.run_status.in_(RunService.TERMINAL_RUN_STATUSES))
            .filter(
                func.coalesce(Run.ended_at, Run.updated_at, Run.created_at) >= since
            )
        )
        if env:
            query = query.filter(Run.env == env)
        query = query.order_by(Run.run_id.desc()).limit(
            self._agent_scheduling_feedback_max_runs()
        )

        host_set = set(hosts)
        feedback: dict[str, dict[str, Any]] = {
            host: {
                "recent_success_count": 0,
                "recent_failure_count": 0,
                "recent_failure_reasons": [],
                "feedback_penalty": 0,
            }
            for host in hosts
        }

        for run in query.all():
            run_hosts = [
                host
                for host in self._extract_agent_hosts_from_run(run)
                if host in host_set
            ]
            if not run_hosts:
                continue
            for host in run_hosts:
                bucket = feedback[host]
                if run.run_status == RunStatus.SUCCEEDED:
                    bucket["recent_success_count"] += 1
                    continue
                bucket["recent_failure_count"] += 1
                reason = (
                    str(
                        run.run_status_detail or run.stop_reason or run.run_status.value
                    )
                    .strip()
                    .replace("\n", " ")
                )
                if reason and reason not in bucket["recent_failure_reasons"]:
                    bucket["recent_failure_reasons"].append(reason[:128])

        compact: dict[str, dict[str, Any]] = {}
        for host, item in feedback.items():
            success_count = int(item["recent_success_count"])
            failure_count = int(item["recent_failure_count"])
            if success_count == 0 and failure_count == 0:
                continue
            item["recent_failure_reasons"] = item["recent_failure_reasons"][:3]
            item["feedback_penalty"] = max(failure_count * 2 - success_count, 0)
            compact[host] = item
        return compact

    def _discover_agent_hosts(self) -> list[str]:
        static_hosts = self._static_agent_hosts()
        try:
            nacos_client = get_nacos_client()
        except Exception as exc:  # pragma: no cover - 容错
            logger.warning("task summary failed to init nacos client: %s", exc)
            nacos_client = None
        has_real_discovery = bool(
            nacos_client and getattr(nacos_client, "client", None) is not None
        )
        if nacos_client:
            try:
                instances = nacos_client.get_service_instances("ptp-agent")
                hosts = [
                    f"{instance.get('ip')}:{int(instance.get('port') or 9096)}"
                    for instance in instances
                    if instance.get("healthy", True) and instance.get("enabled", True)
                ]
                hosts = [host for host in hosts if not host.startswith("None:")]
                if hosts:
                    return list(dict.fromkeys(hosts))
                if static_hosts:
                    logger.info(
                        "task summary discovered zero healthy nacos agents, fallback to static AGENT_HOSTS"
                    )
                    return static_hosts
            except Exception as exc:  # pragma: no cover - 容错
                logger.warning(
                    "task summary failed to discover agents from nacos: %s", exc
                )

        if not has_real_discovery:
            if static_hosts:
                return static_hosts
        return []

    @staticmethod
    def _static_agent_hosts() -> list[str]:
        raw_hosts = os.getenv("AGENT_HOSTS", "")
        hosts = [host.strip() for host in raw_hosts.split(",") if host.strip()]
        return list(dict.fromkeys(hosts))

    def _estimate_in_use_agents(self, env: Optional[str]) -> int:
        running_runs, terminal_runs = self._agent_capacity_candidate_runs(env)

        total = 0
        for run in running_runs:
            total += self._estimate_running_run_usage(run)
        for run in terminal_runs:
            total += self._estimate_recent_terminal_run_usage(run)
        return total

    def _estimate_in_use_agents_by_host(self, env: Optional[str]) -> dict[str, int]:
        running_runs, terminal_runs = self._agent_capacity_candidate_runs(env)
        usage_by_host: dict[str, int] = {}

        for run in running_runs:
            usage = self._estimate_running_run_usage(run)
            self._add_run_usage_by_host(usage_by_host, run, usage)
        for run in terminal_runs:
            usage = self._estimate_recent_terminal_run_usage(run)
            self._add_run_usage_by_host(usage_by_host, run, usage)
        return usage_by_host

    def _agent_capacity_candidate_runs(
        self, env: Optional[str]
    ) -> tuple[list[Run], list[Run]]:
        active_statuses = (RunStatus.PREPARING, RunStatus.RUNNING)
        running_query = self.db.query(Run).filter(Run.run_status.in_(active_statuses))
        if env:
            running_query = running_query.filter(Run.env == env)
        running_runs = running_query.all()

        # terminal-state reconciliation：仅统计 run_status=RUNNING 会让“刚被误写成 failed 但 agent 仍在打流量”
        # 的 run 立刻从 in_use_total 里消失，导致下一次 prepare 被派到已经满载的 agent 上。
        # 这里额外把“近期刚进入 terminal 且 live agent /status 仍报 running”的 run 也算占用，
        # 并按固定窗口控制口径，避免无界扫描。
        recent_window = self._recent_terminal_window_seconds()
        terminal_candidates: list[Run] = []
        if recent_window > 0:
            terminal_candidates.extend(
                self._query_recent_terminal_runs(env, recent_window)
            )
        terminal_candidates.extend(self._query_false_failed_live_candidates(env))

        seen_terminal_run_ids: set[int] = set()
        terminal_runs: list[Run] = []
        for run in terminal_candidates:
            run_id = getattr(run, "run_id", None)
            if not isinstance(run_id, int) or run_id in seen_terminal_run_ids:
                continue
            seen_terminal_run_ids.add(run_id)
            terminal_runs.append(run)

        return running_runs, terminal_runs

    def _add_run_usage_by_host(
        self, usage_by_host: dict[str, int], run: Run, usage: int
    ) -> None:
        if usage <= 0:
            return
        contexts = self._extract_agent_run_contexts(run)
        hosts = list(dict.fromkeys(host for host, _token in contexts if host))
        if not hosts:
            return

        if len(hosts) == 1:
            usage_by_host[hosts[0]] = usage_by_host.get(hosts[0], 0) + usage
            return

        distributable_usage = max(usage, len(hosts))
        base_usage = distributable_usage // len(hosts)
        remainder = distributable_usage % len(hosts)
        for index, host in enumerate(hosts):
            host_usage = base_usage + (1 if index < remainder else 0)
            usage_by_host[host] = usage_by_host.get(host, 0) + host_usage

    def _query_recent_terminal_runs(
        self, env: Optional[str], window_seconds: int
    ) -> list[Run]:
        now = datetime.now(timezone.utc)
        since = now - timedelta(seconds=window_seconds)
        query = (
            self.db.query(Run)
            .filter(Run.run_status.in_(RunService.TERMINAL_RUN_STATUSES))
            .filter(self._recent_terminal_reference_at_or_after(since))
        )
        if env:
            query = query.filter(Run.env == env)
        return query.all()

    def _query_false_failed_live_candidates(self, env: Optional[str]) -> list[Run]:
        """对已知 false-failed 候选保留更长窗口的 live tie-break。

        terminal-state reconciliation 的现场样本里，run 会先被误写成
        `poll_run_status_error:*`，随后仍继续打流量约 30 分钟以上。仅靠 recent-terminal
        的 1800s 窗口会在尾跑最后十几秒重新掉成 idle，因此这里对这类明确可疑的 terminal run
        再给一层更长但仍有界的候选扫描。
        """

        lookback_seconds = self._false_failed_live_lookback_seconds()
        if lookback_seconds <= 0:
            return []
        now = datetime.now(timezone.utc)
        since = now - timedelta(seconds=lookback_seconds)
        query = (
            self.db.query(Run)
            .filter(Run.run_status.in_(RunService.TERMINAL_RUN_STATUSES))
            .filter(Run.stop_reason.like("poll_run_status_error:%"))
            .filter(
                func.coalesce(Run.created_at, Run.started_at, Run.ended_at) >= since
            )
        )
        if env:
            query = query.filter(Run.env == env)
        return query.all()

    def _estimate_running_run_usage(self, run: Run) -> int:
        pod_count = self._estimate_run_pod_count(run)
        live_decision = self._live_run_counts_as_in_use(run)
        if live_decision is True:
            return pod_count
        if live_decision is False:
            return 0
        if self._is_running_record_fresh(run):
            return pod_count
        return 0

    def _estimate_recent_terminal_run_usage(self, run: Run) -> int:
        """terminal-state reconciliation：仅当 live agent /status 仍报 running 时才认为占用槽位。

        - 对已 terminal 的 run 不再应用 `_is_running_record_fresh` 兜底，避免 stale 记录无限撑占。
        - live tie-break 返回 None（agent 不可达 / 拿不到确定状态）时选择“不计入”，因为 DB
          已经是 terminal，只有当证据确认 agent 仍在工作时才保留占用。
        """
        live_decision = self._live_run_counts_as_in_use(run)
        if live_decision is True:
            return self._estimate_run_pod_count(run)
        return 0

    def _live_run_counts_as_in_use(self, run: Run) -> Optional[bool]:
        agent_contexts = self._extract_agent_run_contexts(run)
        if not agent_contexts:
            return None
        saw_running = False
        saw_unknown = False
        for agent_host, run_token in agent_contexts:
            payload = self._fetch_agent_run_status(agent_host, run_token)
            if not isinstance(payload, dict):
                saw_unknown = True
                continue
            status = str(payload.get("status") or "").lower()
            if status == RunStatus.RUNNING.value:
                saw_running = True
                continue
            if status not in {
                "succeeded",
                "failed",
                "stopped",
                "terminated",
                "completed",
            }:
                saw_unknown = True
        if saw_running:
            return True
        if saw_unknown:
            return None
        return False

    def _fetch_agent_run_status(
        self, agent_host: str, run_token: str
    ) -> Optional[dict[str, Any]]:
        try:
            return RunService(self.db)._fetch_agent_json(
                agent_host,
                f"/agent/runs/{run_token}/status",
            )
        except Exception as exc:  # pragma: no cover - 容错
            logger.info(
                "task summary live status fallback for host=%s token=%s: %s",
                agent_host,
                run_token,
                exc,
            )
            return None

    @staticmethod
    def _extract_agent_run_context(run: Run) -> Optional[tuple[str, str]]:
        contexts = TaskService._extract_agent_run_contexts(run)
        return contexts[0] if contexts else None

    @staticmethod
    def _extract_agent_run_contexts(run: Run) -> list[tuple[str, str]]:
        params = run.params if isinstance(run.params, dict) else {}
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

        host = params.get("agent_host") or params.get("agent_ip")
        token = (
            params.get("agent_run_token")
            or params.get("agent_token")
            or params.get("agent_session")
            or params.get("run_token")
        )
        if isinstance(host, str) and host and isinstance(token, str) and token:
            ctx = (host, token)
            if ctx not in seen:
                contexts.append(ctx)
        return contexts

    @staticmethod
    def _extract_agent_hosts_from_run(run: Run) -> list[str]:
        params = run.params if isinstance(run.params, dict) else {}
        hosts: list[str] = []
        seen: set[str] = set()

        agent_runs = params.get("agent_runs")
        if isinstance(agent_runs, list):
            for item in agent_runs:
                if not isinstance(item, dict):
                    continue
                host = item.get("agent_host") or item.get("agent_ip")
                if not isinstance(host, str) or not host or host in seen:
                    continue
                seen.add(host)
                hosts.append(host)

        host = params.get("agent_host") or params.get("agent_ip")
        if isinstance(host, str) and host and host not in seen:
            hosts.append(host)
        return hosts

    @staticmethod
    def _recent_terminal_reference_at_or_after(since: datetime):
        return or_(
            Run.updated_at >= since,
            and_(
                Run.updated_at.is_(None),
                func.coalesce(Run.ended_at, Run.started_at, Run.created_at) >= since,
            ),
        )

    @staticmethod
    def _running_record_freshness_seconds() -> int:
        raw = os.getenv("TASK_RESOURCE_POOL_RUNNING_FRESHNESS_SECONDS", "1800")
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 1800

    @staticmethod
    def _agent_scheduling_feedback_lookback_seconds() -> int:
        raw = os.getenv("AGENT_SCHEDULING_FEEDBACK_LOOKBACK_SECONDS", "86400")
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 86400

    @staticmethod
    def _agent_scheduling_feedback_max_runs() -> int:
        raw = os.getenv("AGENT_SCHEDULING_FEEDBACK_MAX_RUNS", "200")
        try:
            return max(1, min(int(raw), 1000))
        except (TypeError, ValueError):
            return 200

    @staticmethod
    def _recent_terminal_window_seconds() -> int:
        """多长时间以内的 terminal run 仍需要 live tie-break 来决定是否占用槽位。

        默认下限与 `RUNNING` freshness 对齐为 1800s：
        既能覆盖 false-failed live run 的 30 分钟尾跑，又保持有界扫描。
        运维若显式配置更大的窗口可继续放大，但不能把窗口缩回 120s 这类
        无法兜住现场尾跑的值。
        """
        baseline = TaskService._running_record_freshness_seconds()
        raw = os.getenv("TASK_RESOURCE_POOL_RECENT_TERMINAL_SECONDS")
        if raw is None or not str(raw).strip():
            return baseline
        try:
            configured = int(raw)
        except (TypeError, ValueError):
            return baseline
        return max(baseline, configured)

    @staticmethod
    def _false_failed_live_lookback_seconds() -> int:
        """已知 false-failed 候选的最大扫描窗口。

        默认 6 小时，既覆盖 30 分钟以上尾跑，也避免无界回扫整个历史表。
        """

        raw = os.getenv(
            "TASK_RESOURCE_POOL_FALSE_FAILED_LIVE_LOOKBACK_SECONDS", "21600"
        )
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 21600

    def _is_running_record_fresh(self, run: Run) -> bool:
        freshness_window = self._running_record_freshness_seconds()
        if freshness_window <= 0:
            return True
        reference = run.updated_at or run.started_at or run.created_at
        if reference is None:
            return False
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return now - reference <= timedelta(seconds=freshness_window)

    @staticmethod
    def _estimate_run_pod_count(run: Run) -> int:
        params = run.params if isinstance(run.params, dict) else {}
        for key in ("pod_count", "pod_total", "pod_num"):
            value = params.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
        return 1

    def _build_default_run_params(self, task: Task) -> dict[str, Any]:
        params: dict[str, Any] = {
            "pod_count": 1,
        }

        properties = task.properties if isinstance(task.properties, dict) else {}
        raw_pod_count = properties.get("pod_count") or properties.get("pod_num")
        if isinstance(raw_pod_count, (int, float)) and raw_pod_count > 0:
            params["pod_count"] = int(raw_pod_count)

        raw_variables = properties.get("variables")
        if isinstance(raw_variables, dict):
            for key, value in raw_variables.items():
                if (
                    isinstance(key, str)
                    and key
                    and self._is_editable_run_param_key(key)
                ):
                    params[key] = value

        return params

    def _sanitize_run_params(self, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            return {}

        ignored_keys = {
            "summary_metrics",
            "endpoint_trends",
            "checks",
            "k8s_pods",
            "pods",
            "pod_monitor_series",
            "engine_grafana_url",
            "pod_grafana_url",
            "related_monitors",
            "topology_dashboards",
            "server_config_dashboards",
            "trace_link",
            "alert_subscriptions",
            "alert_policies",
            "observability_queries",
            "query_templates",
            "related_apps",
            "related_apps",
            "logs",
            "metrics_s3",
            "checks_s3",
            "log_s3",
            "k8s_log_s3",
        }

        sanitized: dict[str, Any] = {}
        for key, value in params.items():
            if not isinstance(key, str) or not key:
                continue
            if (
                key in ignored_keys
                or key.startswith("seed_")
                or key.startswith("agent_")
            ):
                continue
            sanitized[key] = value
        return sanitized

    @staticmethod
    def _filter_editable_last_run_params(
        params: Optional[dict[str, Any]],
        variable_keys: set[str],
    ) -> dict[str, Any]:
        if not isinstance(params, dict) or not variable_keys:
            return {}

        filtered: dict[str, Any] = {}
        for key in variable_keys:
            if key in params and TaskService._is_editable_run_param_key(key):
                filtered[key] = params[key]
        return filtered

    @staticmethod
    def _is_editable_run_param_key(key: str) -> bool:
        return key.strip().lower() not in MANAGED_RUN_CONTROL_PARAM_KEYS

    @staticmethod
    def _editable_run_param_count(params: dict[str, Any]) -> int:
        return sum(
            1 for key in params.keys() if TaskService._is_editable_run_param_key(key)
        )

    def _build_script_name_map(self, script_ids: set[int]) -> dict[int, str]:
        if not script_ids:
            return {}
        rows = self.db.query(Script).filter(Script.id.in_(script_ids)).all()
        mapping: dict[int, str] = {}
        for row in rows:
            if not isinstance(row.file_path, str):
                continue
            mapping[int(row.id)] = Path(row.file_path).name
        return mapping

    def _extract_task_focus_fields(self, task: Task) -> dict[str, Any]:
        properties = task.properties if isinstance(task.properties, dict) else {}
        related_apps = properties.get("related_apps")
        if isinstance(related_apps, list):
            normalized_related_apps = [
                item.strip()
                for item in related_apps
                if isinstance(item, str) and item.strip()
            ]
        elif isinstance(related_apps, str) and related_apps.strip():
            normalized_related_apps = [
                item.strip() for item in related_apps.split(",") if item.strip()
            ]
        else:
            normalized_related_apps = []

        raw_pod_count = properties.get("pod_count")
        if not isinstance(raw_pod_count, int):
            raw_pod_count = properties.get("pod_num")
        pod_count = raw_pod_count if isinstance(raw_pod_count, int) else None

        return {
            "business_line": (
                properties.get("business_line")
                if isinstance(properties.get("business_line"), str)
                else None
            ),
            "related_apps": normalized_related_apps or None,
            "monitor_link": (
                properties.get("monitor_link")
                if isinstance(properties.get("monitor_link"), str)
                else (
                    properties.get("monitor_dashboard_url")
                    if isinstance(properties.get("monitor_dashboard_url"), str)
                    else None
                )
            ),
            "trace_link": (
                properties.get("trace_link")
                if isinstance(properties.get("trace_link"), str)
                else None
            ),
            "resource_type": (
                properties.get("resource_type")
                if isinstance(properties.get("resource_type"), str)
                else None
            ),
            "cloud_vendor": (
                properties.get("cloud_vendor")
                if isinstance(properties.get("cloud_vendor"), str)
                else None
            ),
            "pod_count": pod_count,
            "data_distribution": (
                properties.get("data_distribution")
                if isinstance(properties.get("data_distribution"), str)
                else None
            ),
            "metrics_enabled": (
                properties.get("metrics_enabled")
                if isinstance(properties.get("metrics_enabled"), bool)
                else None
            ),
            "extra_dashboard_url": (
                properties.get("extra_dashboard_url")
                if isinstance(properties.get("extra_dashboard_url"), str)
                else None
            ),
        }

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

    def _attach_task_display_fields(self, tasks: list[Task]) -> None:
        user_ids: set[int] = set()
        script_ids: set[int] = set()
        task_ids = [int(task.id) for task in tasks if task.id]
        latest_run_by_task = self._build_latest_run_map(
            [task.id for task in tasks if task.id]
        )
        task_assets = (
            self.db.query(TaskAsset)
            .filter(TaskAsset.task_id.in_(task_ids))
            .order_by(TaskAsset.created_at.desc(), TaskAsset.id.desc())
            .all()
            if task_ids
            else []
        )
        asset_map: dict[int, dict[str, list[TaskAsset]]] = {}
        for asset in task_assets:
            if asset.task_id is None:
                continue
            bucket = asset_map.setdefault(int(asset.task_id), {"proto": [], "data": []})
            bucket.setdefault(asset.category, []).append(asset)
        for task in tasks:
            if task.created_by:
                user_ids.add(int(task.created_by))
            if isinstance(task.collaborator_ids, list):
                user_ids.update(
                    int(item) for item in task.collaborator_ids if isinstance(item, int)
                )
            if task.script_id:
                script_ids.add(int(task.script_id))

        user_map = self._build_user_display_map(user_ids)
        script_name_map = self._build_script_name_map(script_ids)
        for task in tasks:
            if task.created_by:
                created_by_id = int(task.created_by)
                task.created_by_name = user_map.get(
                    created_by_id, f"user#{created_by_id}"
                )
            else:
                task.created_by_name = None

            collaborator_names: list[str] = []
            if isinstance(task.collaborator_ids, list):
                for collaborator_id in task.collaborator_ids:
                    if not isinstance(collaborator_id, int):
                        continue
                    collaborator_names.append(
                        user_map.get(collaborator_id, f"user#{collaborator_id}")
                    )
            task.collaborator_names = collaborator_names or None
            task.script_name = script_name_map.get(int(task.script_id))

            latest_run = latest_run_by_task.get(int(task.id)) if task.id else None
            task.last_run_id = latest_run.run_id if latest_run else None
            task.last_run_status = (
                latest_run.run_status.value
                if latest_run and hasattr(latest_run.run_status, "value")
                else None
            )
            task.last_run_status_label = (
                RUN_STATUS_LABELS.get(task.last_run_status or "")
                if task.last_run_status
                else None
            )
            task.last_run_started_at = latest_run.started_at if latest_run else None

            focus_fields = self._extract_task_focus_fields(task)
            for key, value in focus_fields.items():
                setattr(task, key, value)
            task.scenario_quality_lint = ScenarioQualityLintService.evaluate_task(task)
            task_asset_bucket = asset_map.get(int(task.id), {})
            task.proto_assets = list(task_asset_bucket.get("proto", []))
            task.data_assets = list(task_asset_bucket.get("data", []))

    def _build_latest_run_map(self, task_ids: list[int]) -> dict[int, Run]:
        normalized_task_ids = [int(task_id) for task_id in task_ids if int(task_id) > 0]
        if not normalized_task_ids:
            return {}

        rows = (
            self.db.query(Run)
            .filter(Run.task_id.in_(normalized_task_ids))
            .order_by(Run.task_id.asc(), Run.run_id.desc())
            .all()
        )
        latest_by_task: dict[int, Run] = {}
        for row in rows:
            task_id = int(row.task_id)
            if task_id not in latest_by_task:
                latest_by_task[task_id] = row
        return latest_by_task

    def _build_task_version_responses(
        self, items: list[TaskVersionRecord]
    ) -> list[TaskVersionRecordResponse]:
        created_by_ids: set[int] = set()
        for item in items:
            if item.created_by is not None:
                created_by_ids.add(int(item.created_by))

        user_map = self._build_user_display_map(created_by_ids)
        current_history_id = items[0].id if items else None
        responses: list[TaskVersionRecordResponse] = []
        for item in items:
            snapshot = self._load_task_snapshot(item)
            created_by_name = (
                user_map.get(int(item.created_by), f"user#{int(item.created_by)}")
                if item.created_by is not None
                else None
            )
            responses.append(
                TaskVersionRecordResponse(
                    id=item.id,
                    task_id=item.task_id,
                    version=item.version,
                    created_by_name=created_by_name,
                    script_name=(
                        snapshot.get("script_name")
                        if isinstance(snapshot.get("script_name"), str)
                        else None
                    ),
                    is_current=item.id == current_history_id,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                )
            )
        return responses

    def _create_task_version_snapshot(self, task: Task, user_id: Optional[int]) -> None:
        version = self._next_version(task.id)
        script = self._get_script(task.script_id)
        snapshot = self._dump_task_snapshot(task, version=version, script=script)
        history = TaskVersionRecord(
            task_id=task.id,
            version=version,
            created_by=user_id,
            task_snapshot=json.dumps(snapshot, ensure_ascii=False),
            script_content=self._load_script_content(task.script_id),
        )
        self.db.add(history)
        self.db.commit()

    def _next_version(self, task_id: int) -> str:
        latest = (
            self.db.query(TaskVersionRecord)
            .filter(TaskVersionRecord.task_id == task_id)
            .order_by(TaskVersionRecord.id.desc())
            .first()
        )
        if not latest:
            return "v1"
        latest_value = latest.version.strip().lower()
        if latest_value.startswith("v") and latest_value[1:].isdigit():
            return f"v{int(latest_value[1:]) + 1}"
        return f"v{latest.id + 1}"

    def _resolve_latest_version(self, task_id: int) -> str:
        latest = (
            self.db.query(TaskVersionRecord)
            .filter(TaskVersionRecord.task_id == task_id)
            .order_by(TaskVersionRecord.id.desc())
            .first()
        )
        return latest.version if latest else "v1"

    def _get_script(self, script_id: int) -> Optional[Script]:
        return self.db.query(Script).filter(Script.id == script_id).first()

    @staticmethod
    def _normalize_engine_type(engine_type: Any) -> Optional[EngineType]:
        if isinstance(engine_type, EngineType):
            return engine_type
        try:
            return EngineType(str(engine_type))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_script_type(script_type: Any) -> Optional[ScriptType]:
        if isinstance(script_type, ScriptType):
            return script_type
        try:
            return ScriptType(str(script_type))
        except (TypeError, ValueError):
            return None

    def _validate_engine_script_match(
        self,
        engine_type: Any,
        script_id: Optional[int],
    ) -> None:
        if script_id is None:
            return
        normalized_engine_type = self._normalize_engine_type(engine_type)
        expected_script_type = (
            ENGINE_SCRIPT_TYPE_MAP.get(normalized_engine_type)
            if normalized_engine_type is not None
            else None
        )
        if expected_script_type is None:
            return

        script = self._get_script(int(script_id))
        if script is None:
            return
        actual_script_type = self._normalize_script_type(script.script_type)
        if actual_script_type != expected_script_type:
            expected_suffix = ".jmx" if expected_script_type == ScriptType.JMETER else ".js"
            raise ValueError(
                "Task engine_type does not match uploaded script type: "
                f"{normalized_engine_type.value} tasks require {expected_suffix} scripts"
            )

    def _clone_script_for_task(
        self,
        script_id: Optional[int],
        user_id: Optional[int],
    ) -> Optional[int]:
        if script_id is None:
            return None

        script = self._get_script(int(script_id))
        if script is None:
            return None

        existing_task_count = (
            self.db.query(Task).filter(Task.script_id == int(script_id)).count()
        )
        if existing_task_count <= 0:
            return int(script_id)

        content_bytes = self._load_script_bytes(script)
        if content_bytes is None:
            logger.warning(
                "clone_task_script_failed script_id=%s file_path=%s",
                script_id,
                getattr(script, "file_path", None),
            )
            raise ValueError("Task script clone failed")

        file_path, file_size = self._store_script_copy(
            content_bytes=content_bytes,
            script=script,
        )
        cloned = Script(
            name=script.name,
            description=script.description,
            script_type=script.script_type,
            file_path=file_path,
            file_size=file_size,
            content_hash=hashlib.sha256(content_bytes).hexdigest(),
            version=script.version,
            status=script.status,
            tags=deepcopy(script.tags),
            parameters=deepcopy(script.parameters),
            created_by=user_id if user_id is not None else script.created_by,
            last_used_at=None,
        )
        self.db.add(cloned)
        self.db.commit()
        self.db.refresh(cloned)
        return int(cloned.id)

    def _load_script_bytes(self, script: Script) -> Optional[bytes]:
        if isinstance(script.file_path, str) and script.file_path.startswith("s3://"):
            try:
                bucket, key = s3_utils.parse_s3_uri(script.file_path)
                return s3_utils.download_bytes(bucket, key)
            except Exception as exc:
                logger.warning(
                    "load_script_bytes_from_s3_failed script_id=%s file_path=%s error=%s",
                    script.id,
                    script.file_path,
                    exc,
                )
                return None

        try:
            return Path(str(script.file_path)).read_bytes()
        except FileNotFoundError:
            return None

    def _store_script_copy(
        self, *, content_bytes: bytes, script: Script
    ) -> tuple[str, int]:
        stem = Path(script.name or "script").stem or "script"
        suffix = Path(str(script.file_path or "")).suffix or (
            ".jmx"
            if str(getattr(script.script_type, "value", script.script_type)).lower()
            == "jmeter"
            else ".js"
        )
        unique_name = f"{stem}-{uuid4().hex[:8]}"
        use_s3 = (
            settings.USE_S3
            and settings.AWS_ACCESS_KEY_ID
            and settings.AWS_SECRET_ACCESS_KEY
        )
        if use_s3:
            key = f"scripts/{unique_name}{suffix}"
            s3_utils.upload_bytes(
                settings.S3_BUCKET,
                key,
                content_bytes,
                content_type="text/plain; charset=utf-8",
            )
            return f"s3://{settings.S3_BUCKET}/{key}", len(content_bytes)

        stored_path = _task_scripts_dir() / f"{unique_name}{suffix}"
        stored_path.write_bytes(content_bytes)
        return str(stored_path), stored_path.stat().st_size

    def _load_script_content(self, script_id: int) -> str:
        script = self._get_script(script_id)
        if not script:
            return ""

        if isinstance(script.file_path, str) and script.file_path.startswith("s3://"):
            try:
                bucket, key = s3_utils.parse_s3_uri(script.file_path)
                raw = s3_utils.download_bytes(bucket, key)
                return raw.decode("utf-8", errors="replace")
            except Exception as exc:
                logger.warning(
                    "load_script_content_from_s3_failed script_id=%s file_path=%s error=%s",
                    script_id,
                    script.file_path,
                    exc,
                )
                return ""

        try:
            return Path(script.file_path).read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    def _build_script_version_content(
        self,
        version: str,
        script: Optional[Script],
        content: str,
        created_by_name: Optional[str] = None,
        updated_at: Optional[datetime] = None,
    ) -> TaskScriptVersionContent:
        file_name = None
        if script and isinstance(script.file_path, str):
            file_name = Path(script.file_path).name
        return TaskScriptVersionContent(
            version=version,
            content=content,
            file_name=file_name,
            created_by_name=created_by_name,
            updated_at=updated_at,
        )

    def _resolve_task_version_created_by_name(
        self, history: TaskVersionRecord
    ) -> Optional[str]:
        if history.created_by is None:
            return None
        created_by = int(history.created_by)
        return self._build_user_display_map({created_by}).get(
            created_by, f"user#{created_by}"
        )

    def _dump_task_snapshot(
        self, task: Task, version: str, script: Optional[Script]
    ) -> dict[str, Any]:
        return {
            "id": task.id,
            "name": task.name,
            "description": task.description,
            "env": task.env,
            "script_id": task.script_id,
            "script_name": script.name if script else None,
            "engine_type": (
                task.engine_type.value
                if hasattr(task.engine_type, "value")
                else task.engine_type
            ),
            "task_pattern": (
                task.task_pattern.value
                if hasattr(task.task_pattern, "value")
                else task.task_pattern
            ),
            "protocols": task.protocols,
            "thread_count": task.thread_count,
            "duration": task.duration,
            "ramp_up": task.ramp_up,
            "status": (
                task.status.value if hasattr(task.status, "value") else task.status
            ),
            "properties": task.properties,
            "created_by": task.created_by,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "updated_at": task.updated_at.isoformat() if task.updated_at else None,
            "version": version,
        }

    def _load_task_snapshot(self, history: TaskVersionRecord) -> dict[str, Any]:
        try:
            snapshot = json.loads(history.task_snapshot)
        except json.JSONDecodeError:
            return {}
        return snapshot if isinstance(snapshot, dict) else {}
