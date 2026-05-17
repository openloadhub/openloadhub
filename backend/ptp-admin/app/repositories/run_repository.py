from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy import String, cast, or_
from sqlalchemy.orm import Session

from app.models.run import Run
from app.models.task import Task
from app.models.user import User
from common.models.enums import RunStatus


class RunRepository:
    def __init__(self, db: Session):
        self.db = db

    def find_by_id(self, run_id: int) -> Optional[Run]:
        return self.db.query(Run).filter(Run.run_id == run_id).first()

    def find_latest_by_task_id(self, task_id: int) -> Optional[Run]:
        return (
            self.db.query(Run)
            .filter(Run.task_id == task_id)
            .order_by(Run.run_id.desc())
            .first()
        )

    def find_by_idempotency_key_recent(self, key: str, window_seconds: int) -> Optional[Run]:
        threshold = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        return (
            self.db.query(Run)
            .filter(Run.idempotency_key == key)
            .filter(Run.created_at >= threshold)
            .first()
        )

    def find_by_idempotency_key(self, key: str) -> Optional[Run]:
        return self.db.query(Run).filter(Run.idempotency_key == key).first()

    def find_active_runs(self, envs: Optional[list[str]] = None) -> List[Run]:
        query = self.db.query(Run).filter(
            Run.run_status.in_([RunStatus.PREPARING, RunStatus.RUNNING])
        )
        if envs:
            query = query.filter(Run.env.in_(envs))
        return query.order_by(Run.run_id.asc()).all()

    def find_all(
        self,
        page: int,
        page_size: int,
        task_id: Optional[int] = None,
        task_name: Optional[str] = None,
        run_id: Optional[int] = None,
        run_status: Optional[RunStatus] = None,
        engine_type: Optional[str] = None,
        operator_name: Optional[str] = None,
        protocol: Optional[str] = None,
        business_line: Optional[str] = None,
        task_pattern: Optional[str] = None,
        env: Optional[str] = None,
        started_from: Optional[datetime] = None,
        started_to: Optional[datetime] = None,
    ) -> Tuple[List[Run], int]:
        query = self.db.query(Run)
        joined_task = False

        def ensure_task_join():
            nonlocal query, joined_task
            if not joined_task:
                query = query.join(Task, Task.id == Run.task_id)
                joined_task = True

        if task_id is not None:
            query = query.filter(Run.task_id == task_id)
        if task_name:
            query = query.filter(Run.task_name.like(f"%{task_name}%"))
        if run_id is not None:
            query = query.filter(Run.run_id == run_id)
        if run_status is not None:
            query = query.filter(Run.run_status == run_status)
        if engine_type:
            engine_type_terms = [item.strip().lower() for item in engine_type.split(",") if item.strip()]
            if engine_type_terms:
                query = query.filter(cast(Run.engine_type, String).in_(engine_type_terms))
        if protocol:
            ensure_task_join()
            protocol_terms = [item.strip().lower() for item in protocol.split(",") if item.strip()]
            if protocol_terms:
                query = query.filter(
                    or_(
                        *[
                            cast(Task.protocols, String).like(f'%"{item}"%')
                            for item in protocol_terms
                        ]
                    )
                )
        if env:
            ensure_task_join()
            query = query.filter(Task.env == env)
        if operator_name:
            needle = f"%{operator_name.strip()}%"
            ensure_task_join()
            query = query.join(User, User.id == Task.created_by)
            query = query.filter(
                or_(
                    User.full_name.like(needle),
                    User.username.like(needle),
                )
            )
        if business_line:
            ensure_task_join()
            business_line_terms = [item.strip() for item in business_line.split(",") if item.strip()]
            if business_line_terms:
                query = query.filter(Task.properties["business_line"].as_string().in_(business_line_terms))
        if task_pattern:
            ensure_task_join()
            raw_task_pattern = str(getattr(task_pattern, "value", task_pattern) or "")
            task_pattern_terms = [item.strip() for item in raw_task_pattern.split(",") if item.strip()]
            if task_pattern_terms:
                query = query.filter(Task.task_pattern.in_(task_pattern_terms))
        if started_from is not None:
            query = query.filter(Run.started_at >= started_from)
        if started_to is not None:
            query = query.filter(Run.started_at < started_to)

        total = query.count()
        skip = (page - 1) * page_size
        items = query.order_by(Run.run_id.desc()).offset(skip).limit(page_size).all()
        return items, total

    def create(self, run: Run) -> Run:
        self.db.add(run)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            raise
        self.db.refresh(run)
        return run

    def update(self, run: Run) -> Run:
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run
