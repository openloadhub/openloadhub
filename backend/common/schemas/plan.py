from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FieldSerializationInfo,
    field_validator,
    field_serializer,
)

from common.models.enums import (
    PlanExecType,
    PlanRunStatus,
    PlanStageItemType,
    PlanStatus,
    TaskPattern,
)
from common.schemas.task import ScenarioQualityLint
from common.utils.time import to_rfc3339_z


class OnFailureStrategy(str, Enum):
    """失败策略"""

    ABORT = "ABORT"  # 中止整个 PlanRun，状态置为 FAILED
    SKIP = "SKIP"  # 跳过当前 stage 剩余 item，进入下一 stage
    CONTINUE = "CONTINUE"  # 忽略失败，继续当前 stage 下一个 item


class PlanStageItemRoundOverride(BaseModel):
    round: int = Field(..., ge=1)
    run_params: Optional[dict[str, Any]] = None
    post_params: Optional[dict[str, Any]] = None


class PlanStageItem(BaseModel):
    type: PlanStageItemType
    task_id: Optional[int] = None
    task_name: Optional[str] = None
    task_pattern: Optional[TaskPattern] = None
    run_params: Optional[dict[str, Any]] = None
    post_params: Optional[dict[str, Any]] = None
    round_overrides: Optional[list[PlanStageItemRoundOverride]] = None
    # 重试配置
    retry_count: int = Field(0, ge=0, description="失败重试次数")
    retry_delay_seconds: int = Field(10, ge=0, description="重试间隔(秒)")
    on_failure: OnFailureStrategy = Field(
        OnFailureStrategy.ABORT, description="失败策略"
    )


class PlanStage(BaseModel):
    stage: int = Field(..., ge=0)
    items: list[PlanStageItem] = Field(..., min_length=1)


class PlanCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None

    status: PlanStatus = PlanStatus.READY
    exec_type: PlanExecType = PlanExecType.MANUAL
    cron: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    timezone: Optional[str] = None

    enable_round: Optional[bool] = None
    total_round: Optional[int] = None
    business_lines: Optional[list[str]] = None
    collaborator_ids: Optional[list[int]] = None
    stages: list[PlanStage] = Field(..., min_length=1)

    model_config = ConfigDict()

    @field_validator("status")
    @classmethod
    def _reject_deleted_status(cls, value: PlanStatus) -> PlanStatus:
        if value == PlanStatus.DELETED:
            raise ValueError("deleted status is only allowed through delete API")
        return value


class PlanUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None

    status: Optional[PlanStatus] = None
    exec_type: Optional[PlanExecType] = None
    cron: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    timezone: Optional[str] = None

    enable_round: Optional[bool] = None
    total_round: Optional[int] = None
    business_lines: Optional[list[str]] = None
    collaborator_ids: Optional[list[int]] = None
    stages: Optional[list[PlanStage]] = None

    model_config = ConfigDict()

    @field_validator("status")
    @classmethod
    def _reject_deleted_status(cls, value: Optional[PlanStatus]) -> Optional[PlanStatus]:
        if value == PlanStatus.DELETED:
            raise ValueError("deleted status is only allowed through delete API")
        return value


class _RFC3339BaseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @field_serializer("*", when_used="json")
    def _serialize_datetimes(self, value: Any, info: FieldSerializationInfo) -> Any:
        if isinstance(value, datetime):
            return to_rfc3339_z(value)
        return value


class PlanExecutionEstimate(_RFC3339BaseModel):
    round_count: int = 1
    single_round_seconds: int = 0
    total_seconds: int = 0
    task_seconds: int = 0
    interval_seconds: int = 0
    stage_seconds: list[int] = Field(default_factory=list)
    deterministic: bool = False
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None


class PlanResponse(_RFC3339BaseModel):
    plan_id: int
    name: str
    description: Optional[str] = None

    status: PlanStatus
    status_label: Optional[str] = None
    exec_type: PlanExecType
    exec_type_label: Optional[str] = None
    cron: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    timezone: Optional[str] = None

    enable_round: Optional[bool] = None
    total_round: Optional[int] = None
    round_mode_label: Optional[str] = None
    stages: list[PlanStage]
    stage_total: Optional[int] = None
    task_total: Optional[int] = None
    postprocessor_total: Optional[int] = None
    env_list: Optional[list[str]] = None
    business_lines: Optional[list[str]] = None
    engine_types: Optional[list[str]] = None
    collaborator_ids: Optional[list[int]] = None
    collaborator_names: Optional[list[str]] = None
    secondary_summary: Optional[str] = None
    execution_estimate: Optional[PlanExecutionEstimate] = None
    scenario_quality_lint: Optional[ScenarioQualityLint] = None
    latest_plan_run_id: Optional[int] = None
    latest_plan_run_status: Optional[PlanRunStatus] = None
    latest_plan_run_status_label: Optional[str] = None
    latest_plan_run_started_at: Optional[datetime] = None
    latest_plan_run_ended_at: Optional[datetime] = None

    created_at: datetime
    updated_at: Optional[datetime] = None
    created_by: Optional[int] = None
    created_by_name: Optional[str] = None


class PlanExecuteRequest(BaseModel):
    start_type: Optional[int] = None


class PlanExecuteResponse(BaseModel):
    plan_run_id: int
    status: PlanRunStatus
    start_type: Optional[int] = None


class PlanRunK6BroadcastRequest(BaseModel):
    selected_round: Optional[int] = Field(default=None, ge=1)
    ratio: Optional[float] = Field(default=None, gt=0)
    target_total_tps: Optional[float] = Field(default=None, gt=0)
    async_mode: bool = False


class PlanRunK6BroadcastItem(BaseModel):
    collection_id: Optional[int] = None
    run_id: int
    task_name: Optional[str] = None
    env: Optional[str] = None
    base_target_tps: Optional[float] = None
    target_tps: Optional[float] = None
    status: Optional[str] = None
    success: bool = True
    detail: Optional[str] = None

    model_config = ConfigDict()


class PlanRunK6BroadcastResponse(BaseModel):
    plan_run_id: int
    selected_round: int
    controllable_run_total: int
    success_run_total: int
    failed_run_total: int
    skipped_run_total: int
    ratio: float
    base_total_tps: Optional[float] = None
    target_total_tps: Optional[float] = None
    items: list[PlanRunK6BroadcastItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    model_config = ConfigDict()


class PlanRunK6BroadcastAcceptedResponse(BaseModel):
    plan_run_id: int
    selected_round: int
    accepted: bool = True
    async_task_id: str
    ratio: float
    base_total_tps: Optional[float] = None
    target_total_tps: Optional[float] = None

    model_config = ConfigDict()


class PlanRunK6BroadcastTaskStatusResponse(BaseModel):
    plan_run_id: int
    selected_round: int
    async_task_id: str
    job_status: str
    completed: bool = False
    result: Optional[PlanRunK6BroadcastResponse] = None
    error: Optional[str] = None

    model_config = ConfigDict()


class PlanRunListItem(_RFC3339BaseModel):
    """列表接口使用：不含 stages_snapshot / launched_run_ids"""

    plan_run_id: int
    plan_id: int
    plan_name: Optional[str] = None
    plan_exec_type: Optional[PlanExecType] = None
    plan_exec_type_label: Optional[str] = None

    status: PlanRunStatus
    status_label: Optional[str] = None
    status_detail: Optional[str] = None

    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    round: Optional[int] = None
    round_total: Optional[int] = None
    round_label: Optional[str] = None
    round_mode_label: Optional[str] = None
    stage_total: Optional[int] = None
    task_total: Optional[int] = None
    postprocessor_total: Optional[int] = None
    launched_run_total: Optional[int] = None
    planned_total_seconds: Optional[int] = None
    planned_task_seconds: Optional[int] = None
    planned_interval_seconds: Optional[int] = None
    env_list: Optional[list[str]] = None
    business_lines: Optional[list[str]] = None
    engine_types: Optional[list[str]] = None
    collaborator_names: Optional[list[str]] = None
    summary_label: Optional[str] = None
    report_ready_total: int = 0
    report_template_fallback_total: int = 0
    report_missing_total: int = 0
    advisory_verdict: Optional[str] = None
    advisory_summary: Optional[str] = None
    can_stop: bool = False
    latest_failed_run_id: Optional[int] = None
    latest_failed_task_name: Optional[str] = None

    created_at: datetime
    created_by: Optional[int] = None
    created_by_name: Optional[str] = None


class PlanRunRoundItem(BaseModel):
    round: int
    label: Optional[str] = None
    status: Optional[PlanRunStatus] = None
    status_label: Optional[str] = None

    model_config = ConfigDict()


class PlanRunTaskExecRecord(_RFC3339BaseModel):
    collection_id: Optional[int] = None
    stage: int
    logic_type: str
    task_id: Optional[int] = None
    task_name: Optional[str] = None
    run_id: Optional[int] = None
    status: Optional[str] = None
    status_label: Optional[str] = None
    logic_type_label: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    run_params: Optional[dict[str, Any]] = None
    env: Optional[str] = None
    engine_type: Optional[str] = None
    total_requests: Optional[int] = None
    rps: Optional[float] = None
    error_rate: Optional[float] = None
    avg_rt_ms: Optional[float] = None
    p95_rt_ms: Optional[float] = None
    p99_rt_ms: Optional[float] = None
    success_rate: Optional[float] = None
    metric_summary: Optional[str] = None
    status_reason: Optional[str] = None
    postprocessor_kind: Optional[str] = None
    postprocessor_seconds: Optional[int] = None


class PlanRunExecutionSummary(BaseModel):
    total_records: int
    task_total: int
    postprocessor_total: int
    succeeded_total: int
    failed_total: int
    stopped_total: int
    pending_total: int
    launched_run_total: int
    total_throughput: Optional[float] = None
    weighted_error_rate: Optional[float] = None
    max_avg_rt_ms: Optional[float] = None
    max_p95_rt_ms: Optional[float] = None
    max_p99_rt_ms: Optional[float] = None
    peak_qps: Optional[float] = None

    model_config = ConfigDict()


class PlanRunSelectedRoundSummary(_RFC3339BaseModel):
    round: int
    label: Optional[str] = None
    status: Optional[PlanRunStatus] = None
    status_label: Optional[str] = None
    summary_label: Optional[str] = None
    total_records: int
    task_total: int
    postprocessor_total: int
    succeeded_total: int
    failed_total: int
    stopped_total: int
    pending_total: int
    launched_run_total: int
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None

    model_config = ConfigDict()


class PlanRunSelectedRunSummary(_RFC3339BaseModel):
    run_id: int
    task_name: Optional[str] = None
    status: Optional[str] = None
    status_label: Optional[str] = None
    env: Optional[str] = None
    engine_type: Optional[str] = None
    duration_seconds: Optional[int] = None
    total_requests: Optional[int] = None
    rps: Optional[float] = None
    error_rate: Optional[float] = None
    avg_rt_ms: Optional[float] = None
    p95_rt_ms: Optional[float] = None
    p99_rt_ms: Optional[float] = None
    success_rate: Optional[float] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    metric_summary: Optional[str] = None


class PlanRunSelectedTaskSummary(_RFC3339BaseModel):
    collection_id: Optional[int] = None
    record_index: Optional[int] = None
    record_total: Optional[int] = None
    position_label: Optional[str] = None
    stage: int
    logic_type: str
    logic_type_label: Optional[str] = None
    task_id: Optional[int] = None
    task_name: Optional[str] = None
    run_id: Optional[int] = None
    status: Optional[str] = None
    status_label: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    run_params: Optional[dict[str, Any]] = None
    env: Optional[str] = None
    engine_type: Optional[str] = None
    total_requests: Optional[int] = None
    rps: Optional[float] = None
    error_rate: Optional[float] = None
    avg_rt_ms: Optional[float] = None
    p95_rt_ms: Optional[float] = None
    p99_rt_ms: Optional[float] = None
    success_rate: Optional[float] = None
    metric_summary: Optional[str] = None
    status_reason: Optional[str] = None
    postprocessor_kind: Optional[str] = None
    postprocessor_seconds: Optional[int] = None


class PlanRunSelectedInterfaceSummary(BaseModel):
    collection_id: Optional[int] = None
    run_id: Optional[int] = None
    task_name: Optional[str] = None
    source: str = "none"
    metric_total: int = 0
    total_requests: Optional[int] = None
    total_throughput: Optional[float] = None
    max_avg_rt_ms: Optional[float] = None
    max_p95_rt_ms: Optional[float] = None
    max_p99_rt_ms: Optional[float] = None
    endpoint_preview: list[str] = Field(default_factory=list)
    empty_reason: Optional[str] = None

    model_config = ConfigDict()


class PlanRunReportItem(BaseModel):
    collection_id: Optional[int] = None
    run_id: int
    task_name: Optional[str] = None
    resolution_status: str
    message: str
    report_id: Optional[int] = None
    report_name: Optional[str] = None

    model_config = ConfigDict()


class PlanRunReportSummary(BaseModel):
    linked_run_total: int = 0
    ready_total: int = 0
    template_fallback_total: int = 0
    missing_total: int = 0
    conclusion_status: str = "missing"
    conclusion_text: str
    selected_run_status: Optional[str] = None
    selected_run_report_id: Optional[int] = None
    selected_run_report_name: Optional[str] = None
    selected_run_message: Optional[str] = None
    items: list[PlanRunReportItem] = Field(default_factory=list)

    model_config = ConfigDict()


class PlanRunAdvisoryFinding(BaseModel):
    label: str
    detail: str

    model_config = ConfigDict()


class PlanRunAdvisoryPrimaryFocus(BaseModel):
    kind: str
    label: str
    detail: Optional[str] = None
    collection_id: Optional[int] = None
    run_id: Optional[int] = None
    report_id: Optional[int] = None

    model_config = ConfigDict()


class PlanRunAdvisorySummary(BaseModel):
    verdict: str
    delivery_status: str = "needs_attention"
    delivery_status_label: Optional[str] = None
    evidence_ready: bool = False
    summary_text: str
    input_sources: list[str] = Field(default_factory=list)
    primary_focus: Optional[PlanRunAdvisoryPrimaryFocus] = None
    key_findings: list[PlanRunAdvisoryFinding] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    model_config = ConfigDict()


class PlanRunDetailResponse(PlanRunListItem):
    """详情接口使用：继承列表字段，增加 snapshot 和 launched_run_ids"""

    detail_mode: Optional[str] = None
    task_record_total: Optional[int] = None
    plan_exec_type: Optional[PlanExecType] = None
    launched_run_ids: Optional[list[int]] = None
    stages_snapshot: Optional[list[PlanStage]] = None
    rounds: Optional[list[PlanRunRoundItem]] = None
    task_exec_records: Optional[list[PlanRunTaskExecRecord]] = None
    selected_round: Optional[int] = None
    selected_round_status: Optional[PlanRunStatus] = None
    execution_summary: Optional[PlanRunExecutionSummary] = None
    selected_round_summary: Optional[PlanRunSelectedRoundSummary] = None
    selected_collection_id: Optional[int] = None
    selected_task_summary: Optional[PlanRunSelectedTaskSummary] = None
    selected_interface_summary: Optional[PlanRunSelectedInterfaceSummary] = None
    selected_run_summary: Optional[PlanRunSelectedRunSummary] = None
    report_summary: Optional[PlanRunReportSummary] = None
    advisory_summary: Optional[PlanRunAdvisorySummary] = None
    env_list: Optional[list[str]] = None
    business_lines: Optional[list[str]] = None
    engine_types: Optional[list[str]] = None
    collaborator_ids: Optional[list[int]] = None
    collaborator_names: Optional[list[str]] = None


# 保留向后兼容别名
PlanRunResponse = PlanRunDetailResponse


__all__ = [
    "OnFailureStrategy",
    "PlanStageItem",
    "PlanStageItemRoundOverride",
    "PlanStage",
    "PlanCreate",
    "PlanUpdate",
    "PlanExecutionEstimate",
    "PlanResponse",
    "PlanExecuteRequest",
    "PlanExecuteResponse",
    "PlanRunListItem",
    "PlanRunRoundItem",
    "PlanRunTaskExecRecord",
    "PlanRunExecutionSummary",
    "PlanRunSelectedRoundSummary",
    "PlanRunSelectedTaskSummary",
    "PlanRunSelectedInterfaceSummary",
    "PlanRunSelectedRunSummary",
    "PlanRunReportItem",
    "PlanRunReportSummary",
    "PlanRunAdvisoryFinding",
    "PlanRunAdvisoryPrimaryFocus",
    "PlanRunAdvisorySummary",
    "PlanRunDetailResponse",
    "PlanRunResponse",
    "PlanStatus",
    "PlanExecType",
    "PlanRunStatus",
    "PlanStageItemType",
]
