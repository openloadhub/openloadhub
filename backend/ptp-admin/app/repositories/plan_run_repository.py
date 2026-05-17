from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Sequence, Tuple

from sqlalchemy import String, and_, cast, false, func, or_
from sqlalchemy.orm import Session, load_only

from app.models.plan import Plan
from app.models.plan_run import PlanRun
from app.models.task import Task
from common.models.enums import PlanRunStatus

MIXED_RUN_PLAN_DOMAIN = "mixed_run"
MIXED_PRESET_PLAN_DOMAIN = "mixed_preset"
HIDDEN_PLAN_DOMAINS = {MIXED_RUN_PLAN_DOMAIN, MIXED_PRESET_PLAN_DOMAIN}


class PlanRunRepository:
    def __init__(self, db: Session):
        self.db = db

    def find_by_id(self, plan_run_id: int) -> Optional[PlanRun]:
        return self.db.query(PlanRun).filter(PlanRun.plan_run_id == plan_run_id).first()

    def find_all(
        self,
        page: int,
        page_size: int,
        plan_run_id: Optional[int] = None,
        plan_id: Optional[int] = None,
        plan_ids: Optional[Sequence[int]] = None,
        plan_name: Optional[str] = None,
        status: Optional[PlanRunStatus] = None,
        created_by: Optional[int] = None,
        created_by_ids: Optional[Sequence[int]] = None,
        started_from: Optional[datetime] = None,
        started_to: Optional[datetime] = None,
        domain_scope: str = "all",
    ) -> Tuple[List[PlanRun], int]:
        query = self._build_filtered_query(
            plan_run_id=plan_run_id,
            plan_id=plan_id,
            plan_ids=plan_ids,
            plan_name=plan_name,
            status=status,
            created_by=created_by,
            created_by_ids=created_by_ids,
            started_from=started_from,
            started_to=started_to,
            domain_scope=domain_scope,
        )

        total = query.count()
        skip = (page - 1) * page_size
        items = (
            query.order_by(PlanRun.plan_run_id.desc())
            .offset(skip)
            .limit(page_size)
            .all()
        )
        return items, total

    def find_filter_candidates(
        self,
        plan_run_id: Optional[int] = None,
        plan_id: Optional[int] = None,
        plan_ids: Optional[Sequence[int]] = None,
        plan_name: Optional[str] = None,
        business_line: Optional[str] = None,
        env: Optional[str] = None,
        engine_type: Optional[str] = None,
        round_mode: Optional[str] = None,
        status: Optional[PlanRunStatus] = None,
        created_by: Optional[int] = None,
        created_by_ids: Optional[Sequence[int]] = None,
        started_from: Optional[datetime] = None,
        started_to: Optional[datetime] = None,
        domain_scope: str = "all",
    ) -> List[PlanRun]:
        return (
            self._build_filtered_query(
                plan_run_id=plan_run_id,
                plan_id=plan_id,
                plan_ids=plan_ids,
                plan_name=plan_name,
                business_line=business_line,
                env=env,
                engine_type=engine_type,
                round_mode=round_mode,
                status=status,
                created_by=created_by,
                created_by_ids=created_by_ids,
                started_from=started_from,
                started_to=started_to,
                domain_scope=domain_scope,
            )
            .options(
                load_only(
                    PlanRun.plan_run_id,
                    PlanRun.plan_id,
                    PlanRun.status,
                    PlanRun.status_detail,
                    PlanRun.launched_run_ids,
                    PlanRun.stages_snapshot,
                    PlanRun.started_at,
                    PlanRun.ended_at,
                    PlanRun.duration_seconds,
                    PlanRun.round,
                    PlanRun.created_by,
                )
            )
            .order_by(PlanRun.plan_run_id.desc())
            .all()
        )

    def find_by_ids(self, plan_run_ids: Sequence[int]) -> List[PlanRun]:
        normalized_ids = self._normalize_ids(plan_run_ids)
        if not normalized_ids:
            return []
        return (
            self.db.query(PlanRun).filter(PlanRun.plan_run_id.in_(normalized_ids)).all()
        )

    def _build_filtered_query(
        self,
        *,
        plan_run_id: Optional[int] = None,
        plan_id: Optional[int] = None,
        plan_ids: Optional[Sequence[int]] = None,
        plan_name: Optional[str] = None,
        business_line: Optional[str] = None,
        env: Optional[str] = None,
        engine_type: Optional[str] = None,
        round_mode: Optional[str] = None,
        status: Optional[PlanRunStatus] = None,
        created_by: Optional[int] = None,
        created_by_ids: Optional[Sequence[int]] = None,
        started_from: Optional[datetime] = None,
        started_to: Optional[datetime] = None,
        domain_scope: str = "all",
    ):
        query = self.db.query(PlanRun)
        joined_plan = False
        if domain_scope in {"plan", "mixed"}:
            query = query.join(Plan, Plan.plan_id == PlanRun.plan_id)
            joined_plan = True
            if domain_scope == "mixed":
                query = query.filter(Plan.domain_type == MIXED_RUN_PLAN_DOMAIN)
            else:
                query = query.filter(
                    or_(
                        Plan.domain_type.is_(None),
                        Plan.domain_type.notin_(tuple(HIDDEN_PLAN_DOMAINS)),
                    )
                )
        if plan_run_id is not None:
            query = query.filter(PlanRun.plan_run_id == plan_run_id)
        if plan_id is not None:
            query = query.filter(PlanRun.plan_id == plan_id)
        elif plan_ids is not None:
            normalized_plan_ids = self._normalize_ids(plan_ids)
            if not normalized_plan_ids:
                query = query.filter(PlanRun.plan_run_id == -1)
            else:
                query = query.filter(PlanRun.plan_id.in_(normalized_plan_ids))
        if plan_name:
            query = query.filter(PlanRun.plan_name.like(f"%{plan_name}%"))
        if status is not None:
            query = query.filter(PlanRun.status == status)
        if created_by is not None:
            query = query.filter(PlanRun.created_by == created_by)
        elif created_by_ids is not None:
            normalized_created_by_ids = self._normalize_ids(created_by_ids)
            if not normalized_created_by_ids:
                query = query.filter(PlanRun.plan_run_id == -1)
            else:
                query = query.filter(PlanRun.created_by.in_(normalized_created_by_ids))
        task_reference_filter = self._build_task_reference_filters(
            business_line=business_line,
            env=env,
            engine_type=engine_type,
        )
        round_mode_filter = self._build_round_mode_filter(round_mode)
        if task_reference_filter is not None or round_mode_filter is not None:
            if not joined_plan:
                query = query.join(Plan, Plan.plan_id == PlanRun.plan_id)
                joined_plan = True
            if task_reference_filter is not None:
                query = query.filter(task_reference_filter)
            if round_mode_filter is not None:
                query = query.filter(round_mode_filter)
        if started_from is not None:
            query = query.filter(PlanRun.started_at >= started_from)
        if started_to is not None:
            query = query.filter(PlanRun.started_at < started_to)
        return query

    def _normalize_ids(self, raw_ids: Sequence[int]) -> list[int]:
        normalized_ids: list[int] = []
        for raw_id in raw_ids:
            try:
                value = int(raw_id)
            except (TypeError, ValueError):
                continue
            if value > 0:
                normalized_ids.append(value)
        return normalized_ids

    @staticmethod
    def _normalize_json_text(column):
        return func.replace(cast(column, String), " ", "")

    @classmethod
    def _json_object_field_equals_str(cls, column, key: str, value: str):
        escaped_key = str(key).casefold().replace("\\", "\\\\").replace('"', '\\"')
        escaped_value = str(value).casefold().replace("\\", "\\\\").replace('"', '\\"')
        normalized = func.lower(cls._normalize_json_text(column))
        return normalized.like(f'%"{escaped_key}":"{escaped_value}"%')

    @classmethod
    def _json_array_contains_str(cls, column, value: str):
        escaped_value = str(value).casefold().replace("\\", "\\\\").replace('"', '\\"')
        normalized = func.lower(cls._normalize_json_text(column))
        return normalized.like(f'%"{escaped_value}"%')

    @classmethod
    def _build_stage_task_reference_filter(cls, column, task_ids: Sequence[int]):
        normalized_ids = sorted({int(task_id) for task_id in task_ids if task_id})
        if not normalized_ids:
            return None
        normalized_stages = cls._normalize_json_text(column)
        task_clauses = [
            or_(
                normalized_stages.like(f'%"task_id":{task_id},%'),
                normalized_stages.like(f'%"task_id":{task_id}}}%'),
                normalized_stages.like(f'%"task_id":"{task_id}"%'),
            )
            for task_id in normalized_ids
            if task_id > 0
        ]
        return or_(*task_clauses) if task_clauses else None

    @classmethod
    def _build_plan_run_task_reference_filter(cls, task_ids: Sequence[int]):
        snapshot_filter = cls._build_stage_task_reference_filter(
            PlanRun.stages_snapshot, task_ids
        )
        plan_filter = cls._build_stage_task_reference_filter(Plan.stages, task_ids)
        clauses = [
            clause for clause in (snapshot_filter, plan_filter) if clause is not None
        ]
        return or_(*clauses) if clauses else None

    def _build_task_reference_filters(
        self,
        *,
        business_line: Optional[str],
        env: Optional[str],
        engine_type: Optional[str],
    ):
        clauses = []

        business_terms = self._normalize_multi_value_terms(business_line)
        if business_terms:
            explicit_plan_filter = or_(
                *[
                    and_(
                        Plan.business_lines.is_not(None),
                        self._json_array_contains_str(Plan.business_lines, term),
                    )
                    for term in sorted(business_terms)
                ]
            )
            task_filter = self._build_plan_run_task_reference_filter(
                self._find_task_ids_by_business_line(business_line) or []
            )
            business_clauses = [explicit_plan_filter]
            if task_filter is not None:
                business_clauses.append(and_(Plan.business_lines.is_(None), task_filter))
            clauses.append(or_(*business_clauses))

        for task_ids in (
            self._find_task_ids_by_env(env),
            self._find_task_ids_by_engine_type(engine_type),
        ):
            if task_ids is None:
                continue
            task_filter = self._build_plan_run_task_reference_filter(task_ids)
            if task_filter is None:
                return false()
            clauses.append(task_filter)
        return and_(*clauses) if clauses else None

    def _find_task_ids_by_business_line(
        self, business_line: Optional[str]
    ) -> Optional[list[int]]:
        terms = self._normalize_multi_value_terms(business_line)
        if not terms:
            return None
        rows = (
            self.db.query(Task.id)
            .filter(
                or_(
                    *[
                        self._json_object_field_equals_str(
                            Task.properties, "business_line", term
                        )
                        for term in sorted(terms)
                    ]
                )
            )
            .all()
        )
        return [int(task_id) for task_id, in rows if task_id is not None]

    def _find_task_ids_by_env(self, env: Optional[str]) -> Optional[list[int]]:
        terms = self._normalize_multi_value_terms(env)
        if not terms:
            return None
        rows = (
            self.db.query(Task.id).filter(func.lower(Task.env).in_(sorted(terms))).all()
        )
        return [int(task_id) for task_id, in rows if task_id is not None]

    def _find_task_ids_by_engine_type(
        self, engine_type: Optional[str]
    ) -> Optional[list[int]]:
        terms = self._normalize_multi_value_terms(engine_type)
        if not terms:
            return None
        rows = (
            self.db.query(Task.id)
            .filter(func.lower(cast(Task.engine_type, String)).in_(sorted(terms)))
            .all()
        )
        return [int(task_id) for task_id, in rows if task_id is not None]

    @staticmethod
    def _normalize_multi_value_terms(raw_value: Optional[str]) -> set[str]:
        normalized_raw = str(getattr(raw_value, "value", raw_value) or "").strip()
        if not normalized_raw:
            return set()
        return {
            item.strip().casefold()
            for item in normalized_raw.split(",")
            if item.strip()
        }

    @staticmethod
    def _build_round_mode_filter(round_mode: Optional[str]):
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
        return false()

    def create(self, plan_run: PlanRun) -> PlanRun:
        self.db.add(plan_run)
        self.db.commit()
        self.db.refresh(plan_run)
        return plan_run

    def update(self, plan_run: PlanRun) -> PlanRun:
        self.db.add(plan_run)
        self.db.commit()
        self.db.refresh(plan_run)
        return plan_run
