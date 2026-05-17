from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple

from sqlalchemy import String, and_, cast, false, func, or_
from sqlalchemy.orm import Session

from app.models.plan import Plan
from app.models.task import Task
from common.models.enums import PlanExecType, PlanStatus

MIXED_RUN_PLAN_DOMAIN = "mixed_run"
MIXED_PRESET_PLAN_DOMAIN = "mixed_preset"
HIDDEN_PLAN_DOMAINS = {MIXED_RUN_PLAN_DOMAIN, MIXED_PRESET_PLAN_DOMAIN}


class PlanRepository:
    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def _normalize_json_text(column):
        return func.replace(cast(column, String), " ", "")

    @classmethod
    def _json_array_contains_int(cls, column, value: int):
        normalized = cls._normalize_json_text(column)
        return or_(
            normalized.like(f"%[{value}]%"),
            normalized.like(f"%[{value},%"),
            normalized.like(f"%,{value},%"),
            normalized.like(f"%,{value}]%"),
        )

    @classmethod
    def _json_array_contains_str(cls, column, value: str):
        escaped_value = str(value).replace("\\", "\\\\").replace('"', '\\"')
        normalized = cls._normalize_json_text(column)
        return normalized.like(f'%"{escaped_value}"%')

    @classmethod
    def _json_object_field_equals_str(cls, column, key: str, value: str):
        escaped_key = str(key).replace("\\", "\\\\").replace('"', '\\"')
        escaped_value = str(value).replace("\\", "\\\\").replace('"', '\\"')
        normalized = cls._normalize_json_text(column)
        return normalized.like(f'%"{escaped_key}":"{escaped_value}"%')

    @classmethod
    def _build_task_reference_filter(cls, task_ids: list[int]):
        if not task_ids:
            return None
        normalized_stages = cls._normalize_json_text(Plan.stages)
        task_clauses = [
            or_(
                normalized_stages.like(f'%"task_id":{task_id},%'),
                normalized_stages.like(f'%"task_id":{task_id}}}%'),
            )
            for task_id in sorted(set(task_ids))
            if task_id > 0
        ]
        return or_(*task_clauses) if task_clauses else None

    def _find_task_ids_by_business_line(self, business_line: str) -> list[int]:
        rows = (
            self.db.query(Task.id)
            .filter(
                self._json_object_field_equals_str(
                    Task.properties, "business_line", business_line
                )
            )
            .all()
        )
        return [int(task_id) for task_id, in rows if task_id is not None]

    def _find_task_ids_by_participant(self, participant_id: int) -> list[int]:
        rows = (
            self.db.query(Task.id)
            .filter(
                self._json_array_contains_int(Task.collaborator_ids, participant_id)
            )
            .all()
        )
        return [int(task_id) for task_id, in rows if task_id is not None]

    def _find_task_ids_by_env(self, env: str) -> list[int]:
        normalized_env = str(env or "").strip().casefold()
        if not normalized_env:
            return []
        rows = (
            self.db.query(Task.id)
            .filter(func.lower(Task.env) == normalized_env)
            .all()
        )
        return [int(task_id) for task_id, in rows if task_id is not None]

    def _find_task_ids_by_engine_type(self, engine_type: str) -> list[int]:
        normalized_engine_type = str(engine_type or "").strip().casefold()
        if not normalized_engine_type:
            return []
        rows = (
            self.db.query(Task.id)
            .filter(func.lower(cast(Task.engine_type, String)) == normalized_engine_type)
            .all()
        )
        return [int(task_id) for task_id, in rows if task_id is not None]

    def _build_business_line_filter(self, business_line: str):
        task_filter = self._build_task_reference_filter(
            self._find_task_ids_by_business_line(business_line)
        )
        clauses = [
            and_(
                Plan.business_lines.is_not(None),
                self._json_array_contains_str(Plan.business_lines, business_line),
            )
        ]
        if task_filter is not None:
            clauses.append(and_(Plan.business_lines.is_(None), task_filter))
        return or_(*clauses)

    def _build_participant_filter(self, participant_id: int):
        task_filter = self._build_task_reference_filter(
            self._find_task_ids_by_participant(participant_id)
        )
        clauses = [
            Plan.created_by == participant_id,
            and_(
                Plan.collaborator_ids.is_not(None),
                self._json_array_contains_int(Plan.collaborator_ids, participant_id),
            ),
        ]
        if task_filter is not None:
            clauses.append(and_(Plan.collaborator_ids.is_(None), task_filter))
        return or_(*clauses)

    def _build_env_filter(self, env: str):
        task_filter = self._build_task_reference_filter(self._find_task_ids_by_env(env))
        return task_filter if task_filter is not None else false()

    def _build_engine_type_filter(self, engine_type: str):
        task_filter = self._build_task_reference_filter(
            self._find_task_ids_by_engine_type(engine_type)
        )
        return task_filter if task_filter is not None else false()

    @staticmethod
    def _build_round_mode_filter(round_mode: str):
        normalized_round_mode = str(round_mode or "").strip().casefold()
        if not normalized_round_mode:
            return None
        if normalized_round_mode in {"single", "单轮"}:
            return or_(
                Plan.enable_round.is_(False),
                Plan.enable_round.is_(None),
                Plan.total_round.is_(None),
                Plan.total_round <= 1,
            )
        if normalized_round_mode in {"multi", "轮次编排"}:
            return and_(Plan.enable_round.is_(True), Plan.total_round > 1)
        digits = "".join(ch for ch in normalized_round_mode if ch.isdigit())
        if digits:
            return and_(Plan.enable_round.is_(True), Plan.total_round == int(digits))
        return None

    def find_by_id(self, plan_id: int) -> Optional[Plan]:
        return (
            self.db.query(Plan)
            .filter(Plan.plan_id == plan_id, Plan.status != PlanStatus.DELETED)
            .first()
        )

    def find_by_id_including_deleted(self, plan_id: int) -> Optional[Plan]:
        return self.db.query(Plan).filter(Plan.plan_id == plan_id).first()

    def find_all(
        self,
        page: int,
        page_size: int,
        plan_id: Optional[int] = None,
        name: Optional[str] = None,
        status: Optional[PlanStatus] = None,
        exec_type: Optional[PlanExecType] = None,
        created_by: Optional[int] = None,
        created_by_ids: Optional[list[int]] = None,
        participant_id: Optional[int] = None,
        business_line: Optional[str] = None,
        env: Optional[str] = None,
        engine_type: Optional[str] = None,
        round_mode: Optional[str] = None,
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
        time_field: Optional[str] = None,
    ) -> Tuple[List[Plan], int]:
        query = self.db.query(Plan).filter(
            Plan.status != PlanStatus.DELETED,
            or_(Plan.domain_type.is_(None), Plan.domain_type.notin_(tuple(HIDDEN_PLAN_DOMAINS))),
        )
        if plan_id is not None:
            query = query.filter(Plan.plan_id == plan_id)
        if name:
            query = query.filter(Plan.name.like(f"%{name}%"))
        if status is not None:
            query = query.filter(Plan.status == status)
        if exec_type is not None:
            query = query.filter(Plan.exec_type == exec_type)
        if created_by is not None:
            query = query.filter(Plan.created_by == created_by)
        if created_by_ids is not None:
            if not created_by_ids:
                return [], 0
            query = query.filter(Plan.created_by.in_(created_by_ids))
        if participant_id is not None:
            query = query.filter(self._build_participant_filter(participant_id))
        if business_line:
            query = query.filter(self._build_business_line_filter(business_line))
        if env:
            query = query.filter(self._build_env_filter(env))
        if engine_type:
            query = query.filter(self._build_engine_type_filter(engine_type))
        if round_mode:
            round_mode_filter = self._build_round_mode_filter(round_mode)
            if round_mode_filter is None:
                return [], 0
            query = query.filter(round_mode_filter)
        time_column = (
            Plan.updated_at
            if str(time_field or "").strip().lower() == "updated_at"
            else Plan.created_at
        )
        if created_from is not None:
            query = query.filter(time_column >= created_from)
        if created_to is not None:
            query = query.filter(time_column < created_to)

        total = query.count()
        skip = (page - 1) * page_size
        items = (
            query.order_by(Plan.updated_at.desc(), Plan.plan_id.desc())
            .offset(skip)
            .limit(page_size)
            .all()
        )
        return items, total

    def create(self, plan: Plan) -> Plan:
        self.db.add(plan)
        self.db.commit()
        self.db.refresh(plan)
        return plan

    def update(self, plan: Plan) -> Plan:
        self.db.add(plan)
        self.db.commit()
        self.db.refresh(plan)
        return plan

    def delete(self, plan_id: int) -> bool:
        plan = self.find_by_id(plan_id)
        if not plan:
            return False
        plan.status = PlanStatus.DELETED
        self.db.commit()
        return True
