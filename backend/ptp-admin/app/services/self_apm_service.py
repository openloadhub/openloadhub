from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.schemas.self_apm import (
    SelfApmCelerySummaryResponse,
    SelfApmExternalSummaryResponse,
    SelfApmReportSummaryResponse,
    SelfApmSummaryResponse,
)


class SelfApmService:
    """No-op self-observability collector for the public alpha build."""

    @classmethod
    def record_request(cls, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

    @classmethod
    def record_task(cls, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

    @classmethod
    def record_external_query(cls, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

    @classmethod
    def get_summary(cls, *, limit: int = 10) -> SelfApmSummaryResponse:
        return SelfApmSummaryResponse(
            request_count=0,
            error_count=0,
            error_rate=0,
            avg_duration_ms=0,
            p95_duration_ms=0,
            max_duration_ms=0,
            route_total=0,
            slow_routes=[],
            sample_limit_per_route=max(limit, 1),
            storage_source="disabled",
        )

    @classmethod
    def get_celery_summary(cls, *, limit: int = 10) -> SelfApmCelerySummaryResponse:
        return SelfApmCelerySummaryResponse(
            task_count=0,
            failure_count=0,
            failure_rate=0,
            avg_duration_ms=0,
            p95_duration_ms=0,
            max_duration_ms=0,
            task_name_total=0,
            slow_tasks=[],
            sample_limit_per_task=max(limit, 1),
            storage_source="disabled",
        )

    @classmethod
    def get_external_summary(cls, *, limit: int = 10) -> SelfApmExternalSummaryResponse:
        return SelfApmExternalSummaryResponse(
            query_count=0,
            failure_count=0,
            failure_rate=0,
            avg_duration_ms=0,
            p95_duration_ms=0,
            max_duration_ms=0,
            query_target_total=0,
            slow_queries=[],
            sample_limit_per_query=max(limit, 1),
            storage_source="disabled",
        )

    @classmethod
    def get_report_summary(cls, *, limit: int = 10) -> SelfApmReportSummaryResponse:
        return SelfApmReportSummaryResponse(
            generated_at=datetime.now(timezone.utc),
            api=cls.get_summary(limit=limit),
            celery=cls.get_celery_summary(limit=limit),
            external=cls.get_external_summary(limit=limit),
        )
