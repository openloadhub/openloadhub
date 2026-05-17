from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional, Tuple

from sqlalchemy import String, cast, func
from sqlalchemy.orm import Session

from app.models.task import Task
from common.models.enums import TaskStatus


class TaskRepository:
    def __init__(self, db: Session):
        self.db = db

    def find_by_id(self, task_id: int) -> Optional[Task]:
        return self.db.query(Task).filter(Task.id == task_id).first()

    def find_all(
        self,
        status: Optional[TaskStatus] = None,
        task_id: Optional[int] = None,
        name: Optional[str] = None,
        protocol: Optional[str] = None,
        engine_type: Optional[str] = None,
        env: Optional[str] = None,
        script_id: Optional[int] = None,
        task_pattern: Optional[str] = None,
        created_by: Optional[int] = None,
        participant_id: Optional[int] = None,
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
        business_line: Optional[str] = None,
        app_name: Optional[str] = None,
        skip: int = 0,
        limit: Optional[int] = 10,
    ) -> Tuple[List[Task], int]:
        query = self.db.query(Task)
        expected_envs = self._normalize_multi_value_filter(env) if env else set()
        expected_statuses = (
            self._normalize_multi_value_filter(status) if status else set()
        )
        expected_engine_types = (
            self._normalize_multi_value_filter(engine_type) if engine_type else set()
        )
        expected_task_patterns = (
            self._normalize_multi_value_filter(task_pattern) if task_pattern else set()
        )
        if task_id is not None:
            query = query.filter(Task.id == task_id)
        if name:
            query = query.filter(Task.name.like(f"%{name}%"))
        if expected_statuses:
            query = query.filter(
                func.lower(cast(Task.status, String)).in_(expected_statuses)
            )
        if expected_envs:
            query = query.filter(func.lower(Task.env).in_(expected_envs))
        if script_id is not None:
            query = query.filter(Task.script_id == script_id)
        if created_by is not None:
            query = query.filter(Task.created_by == created_by)
        if created_from is not None:
            query = query.filter(Task.created_at >= created_from)
        if created_to is not None:
            query = query.filter(Task.created_at < created_to)
        if expected_engine_types:
            query = query.filter(
                func.lower(cast(Task.engine_type, String)).in_(expected_engine_types)
            )
        if expected_task_patterns:
            query = query.filter(
                func.lower(cast(Task.task_pattern, String)).in_(expected_task_patterns)
            )

        python_filters = []
        if protocol:
            normalized_protocols = self._normalize_multi_value_filter(protocol)
            python_filters.append(
                lambda task: isinstance(task.protocols, list)
                and any(
                    str(item).strip().casefold() in normalized_protocols
                    for item in task.protocols
                )
            )
        if business_line:
            expected_business_lines = self._normalize_multi_value_filter(business_line)
            python_filters.append(
                lambda task: self._matches_business_line(task, expected_business_lines)
            )
        if participant_id is not None:
            python_filters.append(
                lambda task: self._matches_participant(task, participant_id)
            )
        if app_name:
            python_filters.append(lambda task: self._matches_app_name(task, app_name))

        ordered_query = query.order_by(Task.updated_at.desc(), Task.id.desc())
        if not python_filters:
            total = query.order_by(None).count()
            paged_query = ordered_query.offset(skip)
            if limit is not None:
                paged_query = paged_query.limit(limit)
            return paged_query.all(), total

        # JSON/list-backed filters stay in Python for cross-dialect compatibility.
        # SQL filters above still narrow the candidate set before this fallback.
        ordered_tasks = ordered_query.all()
        for task_filter in python_filters:
            ordered_tasks = [task for task in ordered_tasks if task_filter(task)]

        total = len(ordered_tasks)
        if limit is None:
            tasks = ordered_tasks[skip:]
        else:
            tasks = ordered_tasks[skip : skip + limit]
        return tasks, total

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

    def _matches_participant(self, task: Task, participant_id: int) -> bool:
        if task.created_by is not None and int(task.created_by) == int(participant_id):
            return True
        collaborators = (
            task.collaborator_ids if isinstance(task.collaborator_ids, list) else []
        )
        for collaborator in collaborators:
            try:
                if int(collaborator) == int(participant_id):
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _matches_business_line(
        self, task: Task, expected_business_lines: set[str]
    ) -> bool:
        if not expected_business_lines:
            return True
        properties = task.properties if isinstance(task.properties, dict) else {}
        candidate = properties.get("business_line")
        if not isinstance(candidate, str):
            return False
        return candidate.strip().casefold() in expected_business_lines

    def _matches_app_name(self, task: Task, app_name: str) -> bool:
        properties = task.properties if isinstance(task.properties, dict) else {}
        normalized = app_name.strip().casefold()
        if not normalized:
            return True

        candidates: list[str] = []
        raw = properties.get("related_apps")
        if isinstance(raw, list):
            candidates.extend(str(item).strip() for item in raw if str(item).strip())
        elif isinstance(raw, str) and raw.strip():
            candidates.extend(part.strip() for part in raw.split(",") if part.strip())
        for key in ("project_name", "app_name"):
            raw = properties.get(key)
            if isinstance(raw, str) and raw.strip():
                candidates.append(raw.strip())

        return any(normalized in candidate.casefold() for candidate in candidates)

    def create(self, task: Task) -> Task:
        self.db.add(task)
        self.db.commit()
        self.db.refresh(task)
        return task

    def update_status(self, task_id: int, status: TaskStatus) -> Optional[Task]:
        task = self.find_by_id(task_id)
        if task:
            task.status = status
            self.db.commit()
            self.db.refresh(task)
        return task

    def update(self, task_id: int, updates: dict[str, Any]) -> Optional[Task]:
        task = self.find_by_id(task_id)
        if not task:
            return None
        for key, value in updates.items():
            setattr(task, key, value)
        self.db.commit()
        self.db.refresh(task)
        return task

    def delete(self, task_id: int) -> bool:
        task = self.find_by_id(task_id)
        if not task:
            return False
        self.db.delete(task)
        self.db.commit()
        return True
