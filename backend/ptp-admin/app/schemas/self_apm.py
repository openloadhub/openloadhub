from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SelfApmRouteMetric(BaseModel):
    method: str
    path: str
    request_count: int = Field(..., ge=0)
    sample_count: int = Field(..., ge=0)
    error_count: int = Field(..., ge=0)
    error_rate: float = Field(..., ge=0)
    avg_duration_ms: float = Field(..., ge=0)
    p95_duration_ms: float = Field(..., ge=0)
    max_duration_ms: float = Field(..., ge=0)
    last_status_code: int | None = None
    last_seen_at: datetime | None = None
    low_sample: bool = False

    model_config = ConfigDict()


class SelfApmSummaryResponse(BaseModel):
    request_count: int = Field(..., ge=0)
    error_count: int = Field(..., ge=0)
    error_rate: float = Field(..., ge=0)
    avg_duration_ms: float = Field(..., ge=0)
    p95_duration_ms: float = Field(..., ge=0)
    max_duration_ms: float = Field(..., ge=0)
    route_total: int = Field(..., ge=0)
    slow_routes: list[SelfApmRouteMetric] = Field(default_factory=list)
    sample_limit_per_route: int = Field(..., ge=1)
    storage_source: str = "memory"

    model_config = ConfigDict()


class SelfApmTaskMetric(BaseModel):
    task_name: str
    task_count: int = Field(..., ge=0)
    failure_count: int = Field(..., ge=0)
    failure_rate: float = Field(..., ge=0)
    avg_duration_ms: float = Field(..., ge=0)
    p95_duration_ms: float = Field(..., ge=0)
    max_duration_ms: float = Field(..., ge=0)
    last_status: str | None = None
    last_task_id: str | None = None
    last_error: str | None = None

    model_config = ConfigDict()


class SelfApmCelerySummaryResponse(BaseModel):
    task_count: int = Field(..., ge=0)
    failure_count: int = Field(..., ge=0)
    failure_rate: float = Field(..., ge=0)
    avg_duration_ms: float = Field(..., ge=0)
    p95_duration_ms: float = Field(..., ge=0)
    max_duration_ms: float = Field(..., ge=0)
    task_name_total: int = Field(..., ge=0)
    slow_tasks: list[SelfApmTaskMetric] = Field(default_factory=list)
    sample_limit_per_task: int = Field(..., ge=1)
    storage_source: str = "memory"

    model_config = ConfigDict()


class SelfApmExternalQueryMetric(BaseModel):
    source: str
    operation: str
    target: str | None = None
    query_count: int = Field(..., ge=0)
    failure_count: int = Field(..., ge=0)
    failure_rate: float = Field(..., ge=0)
    avg_duration_ms: float = Field(..., ge=0)
    p95_duration_ms: float = Field(..., ge=0)
    max_duration_ms: float = Field(..., ge=0)
    last_status: str | None = None
    last_error: str | None = None

    model_config = ConfigDict()


class SelfApmExternalSummaryResponse(BaseModel):
    query_count: int = Field(..., ge=0)
    failure_count: int = Field(..., ge=0)
    failure_rate: float = Field(..., ge=0)
    avg_duration_ms: float = Field(..., ge=0)
    p95_duration_ms: float = Field(..., ge=0)
    max_duration_ms: float = Field(..., ge=0)
    query_target_total: int = Field(..., ge=0)
    slow_queries: list[SelfApmExternalQueryMetric] = Field(default_factory=list)
    sample_limit_per_query: int = Field(..., ge=1)
    storage_source: str = "memory"

    model_config = ConfigDict()


class SelfApmReportSummaryResponse(BaseModel):
    generated_at: datetime
    api: SelfApmSummaryResponse
    celery: SelfApmCelerySummaryResponse
    external: SelfApmExternalSummaryResponse

    model_config = ConfigDict()
