from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from common.models.enums import EngineType, Protocol, TaskPattern, TaskStatus
from common.schemas.task_asset import TaskAssetResponse
from common.utils.time import to_rfc3339_z


class TaskCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(default=None)
    env: str = Field(..., min_length=1, max_length=64)
    script_id: int = Field(..., gt=0)
    engine_type: EngineType
    task_pattern: TaskPattern = Field(default=TaskPattern.SCRIPT)
    protocols: list[Protocol] = Field(..., min_length=1)
    thread_count: int = Field(..., gt=0, le=10000)
    duration: Optional[int] = Field(default=None, gt=0, le=86400)
    ramp_up: int = Field(default=0, ge=0)
    properties: Optional[dict[str, Any]] = None
    collaborator_ids: Optional[list[int]] = None

    model_config = ConfigDict(
        populate_by_name=True,
        json_schema_extra={
            "example": {
                "name": "登录接口压测",
                "description": "测试登录接口性能",
                "env": "perf-1",
                "script_id": 1,
                "engine_type": "jmeter",
                "task_pattern": "script",
                "protocols": ["http"],
                "thread_count": 100,
                "duration": 300,
                "ramp_up": 60,
                "properties": {"target_host": "api.example.com"},
            }
        },
    )


class TaskUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = Field(default=None)
    env: Optional[str] = Field(default=None, min_length=1, max_length=64)
    script_id: Optional[int] = Field(default=None, gt=0)
    engine_type: Optional[EngineType] = None
    task_pattern: Optional[TaskPattern] = None
    protocols: Optional[list[Protocol]] = None
    thread_count: Optional[int] = Field(default=None, gt=0, le=10000)
    duration: Optional[int] = Field(default=None, gt=0, le=86400)
    ramp_up: Optional[int] = Field(default=None, ge=0)
    properties: Optional[dict[str, Any]] = None
    collaborator_ids: Optional[list[int]] = None

    model_config = ConfigDict(populate_by_name=True)


class ScenarioQualityLintIssue(BaseModel):
    code: str
    label: str
    detail: str
    severity: str = "warning"
    recommended_action: Optional[str] = None


class ScenarioQualityLint(BaseModel):
    status: str = "clean"
    warning_count: int = 0
    issues: list[ScenarioQualityLintIssue] = Field(default_factory=list)


class TaskResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    env: str
    script_id: int
    status: TaskStatus
    engine_type: EngineType
    task_pattern: TaskPattern
    protocols: list[Protocol]
    thread_count: int
    duration: int
    ramp_up: int
    properties: Optional[dict[str, Any]]
    script_name: Optional[str] = None
    created_by: Optional[int]
    created_by_name: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime]
    collaborator_ids: Optional[list[int]] = None
    collaborator_names: Optional[list[str]] = None
    last_run_at: Optional[datetime] = None
    last_run_id: Optional[int] = None
    last_run_status: Optional[str] = None
    last_run_status_label: Optional[str] = None
    last_run_started_at: Optional[datetime] = None
    business_line: Optional[str] = None
    related_apps: Optional[list[str]] = None
    monitor_link: Optional[str] = None
    trace_link: Optional[str] = None
    resource_type: Optional[str] = None
    cloud_vendor: Optional[str] = None
    pod_count: Optional[int] = None
    data_distribution: Optional[str] = None
    metrics_enabled: Optional[bool] = None
    extra_dashboard_url: Optional[str] = None
    scenario_quality_lint: Optional[ScenarioQualityLint] = None
    proto_assets: list[TaskAssetResponse] = Field(default_factory=list)
    data_assets: list[TaskAssetResponse] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class TaskLastRunParamsResponse(BaseModel):
    task_pattern: TaskPattern
    default_run_params: Optional[dict[str, Any]] = None
    last_run_params: Optional[dict[str, Any]] = None
    param_source: Optional[str] = None
    last_run_id: Optional[int] = None
    last_run_status: Optional[str] = None
    last_run_started_at: Optional[datetime] = None
    param_count: int = 0


class TaskPrepareRunResponse(BaseModel):
    task: TaskResponse
    next_action: Optional[str] = None
    status_hint: Optional[str] = None


class TaskBatchStopRequest(BaseModel):
    task_ids: list[int] = Field(..., min_length=1)
    reason: Optional[str] = None


class TaskBatchStopResponse(BaseModel):
    requested_task_ids: list[int]
    stopped_task_ids: list[int]
    stopped_run_ids: list[int]
    skipped_task_ids: list[int]


class TaskPodCapacityItem(BaseModel):
    cluster_code: str
    cluster_name: str
    available_pod_count: int
    resource_spec: Optional[str] = None
    readonly: bool = False


class TaskResourcePoolSummary(BaseModel):
    standby_total: int = 0
    in_use_total: int = 0
    idle_total: int = 0
    engine_compatibility: list[str] = Field(default_factory=list)
    standby_total_source: Optional[str] = None
    resource_spec: Optional[str] = None


class TaskSummaryResponse(BaseModel):
    task_total: int
    task_success_total: int
    task_failed_total: int
    report_total: int
    pod_capacity: list[TaskPodCapacityItem]
    resource_pool: Optional[TaskResourcePoolSummary] = None


class TaskVersionRecordResponse(BaseModel):
    id: int
    task_id: int
    version: str
    created_by_name: Optional[str] = None
    script_name: Optional[str] = None
    is_current: bool = False
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class TaskVersionDetailResponse(BaseModel):
    version: str
    created_by_name: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    snapshot: dict[str, Any]


class TaskScriptVersionContent(BaseModel):
    version: str
    content: str
    file_name: Optional[str] = None
    created_by_name: Optional[str] = None
    updated_at: Optional[datetime] = None


class TaskScriptCompareResponse(BaseModel):
    current: TaskScriptVersionContent
    history: TaskScriptVersionContent


class TaskListResponse(BaseModel):
    items: list[TaskResponse]
    total: int
    page: int
    page_size: int = Field(..., alias="pageSize")

    model_config = ConfigDict(populate_by_name=True)


__all__ = [
    "TaskCreate",
    "TaskUpdate",
    "TaskResponse",
    "TaskLastRunParamsResponse",
    "TaskPrepareRunResponse",
    "ScenarioQualityLint",
    "ScenarioQualityLintIssue",
    "TaskBatchStopRequest",
    "TaskBatchStopResponse",
    "TaskPodCapacityItem",
    "TaskSummaryResponse",
    "TaskVersionRecordResponse",
    "TaskVersionDetailResponse",
    "TaskScriptVersionContent",
    "TaskScriptCompareResponse",
    "TaskListResponse",
    "EngineType",
    "TaskPattern",
    "Protocol",
    "TaskStatus",
]
