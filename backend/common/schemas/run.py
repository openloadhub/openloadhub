from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from common.models.enums import (
    EngineType,
    RunBaselineScopeType,
    RunBaselineSource,
    RunStatus,
)
from common.utils.time import to_rfc3339_z


class RunCreate(BaseModel):
    task_id: int = Field(..., gt=0)
    params: Optional[dict[str, Any]] = None


class RunStopRequest(BaseModel):
    reason: Optional[str] = None


class RunK6ControlRequest(BaseModel):
    target_tps: Optional[float] = Field(default=None, gt=0)


class RunBaselineSetRequest(BaseModel):
    scope_type: Optional[RunBaselineScopeType] = None
    note: Optional[str] = None
    baseline_source: RunBaselineSource = RunBaselineSource.MANUAL


class RunResponse(BaseModel):
    run_id: int
    task_id: int
    task_name: Optional[str] = None
    params: Optional[dict[str, Any]] = None

    engine_type: EngineType
    protocol: Optional[str] = None
    env: str

    run_status: RunStatus
    run_status_detail: Optional[str] = None
    stop_reason: Optional[str] = None

    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None

    total_requests: Optional[int] = None
    success_rate: Optional[float] = None
    error_rate: Optional[float] = None
    avg_rt_ms: Optional[float] = None
    p95_rt_ms: Optional[float] = None
    p99_rt_ms: Optional[float] = None
    rps: Optional[float] = None
    engine_type_label: Optional[str] = None
    run_status_label: Optional[str] = None
    business_line: Optional[str] = None
    current_task_env: Optional[str] = None
    current_task_business_line: Optional[str] = None
    current_task_pattern: Optional[str] = None
    current_task_protocols: Optional[list[str]] = None
    pod_total: Optional[int] = None
    pod_actual: Optional[int] = None
    pod_completed: Optional[int] = None
    operator_name: Optional[str] = None
    run_window_label: Optional[str] = None
    agent_progress_label: Optional[str] = None
    overview_summary: Optional["RunOverviewSummary"] = None
    analysis_readiness: Optional["RunAnalysisReadiness"] = None

    model_config = ConfigDict(from_attributes=True)


class RunOverviewSummary(BaseModel):
    total_requests: Optional[int] = None
    throughput: Optional[float] = None
    avg_rt_ms: Optional[float] = None
    p95_rt_ms: Optional[float] = None
    error_rate: Optional[float] = None
    checks_success_rate: Optional[float] = None
    endpoint_total: Optional[int] = None
    summary_metrics_label: Optional[str] = None
    checks_summary_label: Optional[str] = None

    model_config = ConfigDict()


class RunAnalysisReadinessSection(BaseModel):
    status: str
    label: str
    detail: Optional[str] = None
    evidence: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)

    model_config = ConfigDict()


class RunAnalysisReadiness(BaseModel):
    status: str
    status_label: str
    evidence_ready: bool = False
    required_sections: dict[str, RunAnalysisReadinessSection] = Field(
        default_factory=dict
    )
    evidence: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)

    model_config = ConfigDict()


class MetricName(str, enum.Enum):
    RPS = "rps"
    RT_AVG_MS = "rt_avg_ms"
    RT_P95_MS = "rt_p95_ms"
    RT_P99_MS = "rt_p99_ms"
    ERROR_RATE = "error_rate"


class MetricPoint(BaseModel):
    ts: datetime
    value: Optional[float] = None

    model_config = ConfigDict()


class MetricsSeries(BaseModel):
    metric: MetricName
    unit: str
    points: list[MetricPoint]


class MetricsResponse(BaseModel):
    step_seconds: int = 10
    series: list[MetricsSeries] = Field(default_factory=list)


class LogItem(BaseModel):
    seq: int
    ts: datetime
    level: str
    message: str
    source: Optional[str] = None
    raw_message: Optional[str] = None
    agent_host: Optional[str] = None
    agent_ip: Optional[str] = None
    source_kind: Optional[str] = None
    channel: Optional[str] = None

    model_config = ConfigDict()


class LogsResponse(BaseModel):
    items: list[LogItem] = Field(default_factory=list)
    next_cursor: Optional[str] = None


# === Summary Metrics (接口级核心指标表) ===


class RunSummaryMetricRow(BaseModel):
    """单接口/endpoint级别的聚合指标行"""

    endpoint_name: str
    avg_rt_ms: Optional[float] = None
    p95_rt_ms: Optional[float] = None
    p99_rt_ms: Optional[float] = None
    max_rt_ms: Optional[float] = None
    min_rt_ms: Optional[float] = None
    total_requests: Optional[int] = None
    throughput: Optional[float] = None  # TPS / rep/s

    model_config = ConfigDict()


class RunSummaryMetricsResponse(BaseModel):
    """接口级核心指标表响应"""

    items: list[RunSummaryMetricRow] = Field(default_factory=list)


# === Checks (K6 Group-Checks) ===


class RunCheckRow(BaseModel):
    """K6 checks 或断言聚合行"""

    group_name: str
    check_name: str
    success_rate: Optional[float] = None  # 0~1

    model_config = ConfigDict()


class RunChecksResponse(BaseModel):
    """Group-Checks 响应"""

    items: list[RunCheckRow] = Field(default_factory=list)


# === Pod Status ===


class RunPodStatus(BaseModel):
    """执行节点/Pod 状态"""

    agent_host: Optional[str] = None
    pod_ip: Optional[str] = None
    pod_name: Optional[str] = None
    status: str
    cluster_name: Optional[str] = None
    node_name: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None

    model_config = ConfigDict()


class RunPodStatusResponse(BaseModel):
    """执行节点状态列表响应"""

    items: list[RunPodStatus] = Field(default_factory=list)


# === Pod Monitor ===


class RunPodMonitorMetric(str, enum.Enum):
    CPU_USAGE_PERCENT = "cpu_usage_percent"
    CPU_LIMIT_CORES = "cpu_limit_cores"
    CPU_LOAD = "cpu_load"
    MEMORY_USAGE_PERCENT = "memory_usage_percent"
    MEMORY_USED_BYTES = "memory_used_bytes"
    SOCKET_COUNT = "socket_count"
    DISK_USAGE_PERCENT = "disk_usage_percent"
    DISK_USED_BYTES = "disk_used_bytes"
    DISK_TOTAL_BYTES = "disk_total_bytes"
    NETWORK_RX_BYTES = "network_rx_bytes"
    NETWORK_TX_BYTES = "network_tx_bytes"
    NETWORK_RX_PACKETS = "network_rx_packets"
    NETWORK_TX_PACKETS = "network_tx_packets"


class RunPodMonitorSeries(BaseModel):
    """单个 Pod 的监控时序"""

    agent_host: Optional[str] = None
    pod_name: Optional[str] = None
    pod_ip: Optional[str] = None
    metric: RunPodMonitorMetric
    unit: str
    points: list[MetricPoint] = Field(default_factory=list)

    model_config = ConfigDict()


class RunPodMonitorSummary(BaseModel):
    """执行节点监控摘要"""

    observed_pod_total: int = 0
    runtime_kind: Optional[str] = None
    resource_scope_label: Optional[str] = None
    cpu_usage_peak_percent: Optional[float] = None
    memory_usage_peak_percent: Optional[float] = None
    socket_peak: Optional[float] = None
    cpu_load_latest: Optional[float] = None
    network_rx_peak_bytes: Optional[float] = None
    network_tx_peak_bytes: Optional[float] = None
    cpu_summary_label: Optional[str] = None
    memory_summary_label: Optional[str] = None
    network_summary_label: Optional[str] = None
    runtime_summary_label: Optional[str] = None

    model_config = ConfigDict()


class RunPodMonitorResponse(BaseModel):
    """执行节点资源监控响应"""

    step_seconds: int = 10
    series: list[RunPodMonitorSeries] = Field(default_factory=list)
    summary: RunPodMonitorSummary = Field(default_factory=RunPodMonitorSummary)


# === Dashboards ===


class RunDashboardType(str, enum.Enum):
    RELATED_MONITOR = "related_monitor"
    POD_GRAFANA = "pod_grafana"
    ENGINE_GRAFANA = "engine_grafana"
    TOPOLOGY = "topology"
    SERVER_CONFIG = "server_config"


class RunDashboardLink(BaseModel):
    """外部监控看板入口"""

    dashboard_type: RunDashboardType
    title: str
    url: str
    embed_mode: Optional[str] = "iframe"  # iframe | new_tab

    model_config = ConfigDict()


class RunDashboardSummary(BaseModel):
    has_engine_grafana: bool = False
    has_pod_grafana: bool = False
    related_monitor_total: int = 0
    topology_total: int = 0
    server_config_total: int = 0
    total_dashboard_count: int = 0
    engine_grafana_label: Optional[str] = None
    pod_grafana_label: Optional[str] = None
    related_monitor_label: Optional[str] = None
    topology_label: Optional[str] = None
    server_config_label: Optional[str] = None

    model_config = ConfigDict()


class RunDashboardsResponse(BaseModel):
    """关联监控看板响应"""

    items: list[RunDashboardLink] = Field(default_factory=list)
    summary: RunDashboardSummary = Field(default_factory=RunDashboardSummary)


# === Endpoint Trend (接口级趋势数据) ===


class EndpointTrendMetric(str, enum.Enum):
    """接口趋势指标类型"""

    RT_AVG_MS = "rt_avg_ms"
    RT_P95_MS = "rt_p95_ms"
    RT_P99_MS = "rt_p99_ms"
    THROUGHPUT = "throughput"
    ERROR_RATE = "error_rate"


class EndpointTrendSeries(BaseModel):
    """单个接口的趋势时序数据"""

    endpoint_name: str
    metric: EndpointTrendMetric
    unit: str
    points: list[MetricPoint] = Field(default_factory=list)

    model_config = ConfigDict()


class EndpointTrendResponse(BaseModel):
    """接口级趋势数据响应（支持多接口多折线图）"""

    step_seconds: int = 10
    items: list[EndpointTrendSeries] = Field(default_factory=list)


# === Process (运行过程) ===


class RunProcessStage(BaseModel):
    """运行阶段信息"""

    name: str
    status: str  # pending, running, completed, failed
    progress: Optional[int] = None  # 0-100
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    message: Optional[str] = None

    model_config = ConfigDict()


class RunProcessResponse(BaseModel):
    """运行过程响应"""

    run_status: RunStatus
    run_status_detail: Optional[str] = None
    stages: list[RunProcessStage] = Field(default_factory=list)


class RunK6ScenarioConfig(BaseModel):
    scenario_name: str
    executor: Optional[str] = None
    rate: Optional[int] = None
    time_unit: Optional[str] = None
    pre_allocated_vus: Optional[int] = None
    max_vus: Optional[int] = None
    vus: Optional[int] = None
    duration: Optional[str] = None
    exec_name: Optional[str] = None

    model_config = ConfigDict()


class RunK6ControlAgentState(BaseModel):
    agent_host: Optional[str] = None
    available: bool = False
    reason: Optional[str] = None
    supports_target_tps: bool = False
    observed_tps: Optional[float] = None
    active_vus: Optional[int] = None
    scenario_pre_allocated_vus: Optional[int] = None
    scenario_max_vus: Optional[int] = None
    current_vus: Optional[int] = None
    current_max_vus: Optional[int] = None
    target_tps: Optional[float] = None
    controller_enabled: bool = False
    controller_status: Optional[str] = None
    controller_message: Optional[str] = None
    metric_family: Optional[str] = None
    last_synced_at: Optional[datetime] = None
    control_strategy: Optional[str] = None
    preferred_control_path: Optional[str] = None
    active_control_path: Optional[str] = None
    scenario_patch_supported: bool = False
    scenario_patch_reason: Optional[str] = None
    script_family: Optional[str] = None
    scenario_configs: list[RunK6ScenarioConfig] = Field(default_factory=list)

    model_config = ConfigDict()


class RunK6ControlSummary(BaseModel):
    agent_total: int = 0
    controllable_agent_total: int = 0
    supports_target_tps: bool = False
    observed_tps: Optional[float] = None
    active_vus: Optional[int] = None
    scenario_pre_allocated_vus: Optional[int] = None
    scenario_max_vus: Optional[int] = None
    current_vus: Optional[int] = None
    current_max_vus: Optional[int] = None
    target_tps: Optional[float] = None
    controller_enabled: bool = False
    controller_status: Optional[str] = None
    controller_message: Optional[str] = None
    last_synced_at: Optional[datetime] = None
    control_strategy: Optional[str] = None
    preferred_control_path: Optional[str] = None
    active_control_path: Optional[str] = None
    scenario_patch_supported: bool = False
    scenario_patch_reason: Optional[str] = None
    script_family: Optional[str] = None

    model_config = ConfigDict()


class RunK6ControlResponse(BaseModel):
    run_id: int
    engine_type: str = "k6"
    available: bool = False
    reason: Optional[str] = None
    summary: RunK6ControlSummary = Field(default_factory=RunK6ControlSummary)
    agents: list[RunK6ControlAgentState] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    model_config = ConfigDict()


class RunK6ControlAcceptedResponse(BaseModel):
    run_id: int
    accepted: bool = True
    async_task_id: str
    target_tps: Optional[float] = None

    model_config = ConfigDict()


class RunK6ControlTaskStatusResponse(BaseModel):
    run_id: int
    async_task_id: str
    job_status: str
    completed: bool = False
    result: Optional[RunK6ControlResponse] = None
    error: Optional[str] = None

    model_config = ConfigDict()


class RunCompareBundle(BaseModel):
    detail: RunResponse
    overview_summary: RunOverviewSummary
    baseline: Optional["RunBaselineResponse"] = None
    summary: RunSummaryMetricsResponse
    checks: RunChecksResponse
    metrics: MetricsResponse
    dashboards: RunDashboardsResponse
    endpoint_trends: dict[str, EndpointTrendResponse] = Field(default_factory=dict)


class RunCompareResponse(BaseModel):
    base_id: int
    comparator_id: int
    base: RunCompareBundle
    comparator: RunCompareBundle


class RunBaselineRunSummary(BaseModel):
    run_id: int
    task_id: int
    task_name: Optional[str] = None
    env: str
    protocol: Optional[str] = None
    run_status: RunStatus
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class RunBaselineResponse(BaseModel):
    baseline_id: int
    scope_type: RunBaselineScopeType
    scope_key: str
    scope_label: str
    task_id: int
    env: str
    protocol: Optional[str] = None
    baseline_run_id: int
    baseline_source: RunBaselineSource
    effective_from: Optional[datetime] = None
    note: Optional[str] = None
    current_run_id: Optional[int] = None
    current_run_matches_baseline: bool = False
    baseline_run: Optional[RunBaselineRunSummary] = None

    model_config = ConfigDict()


class RunVerdictMetricDelta(BaseModel):
    metric: str
    unit: Optional[str] = None
    current_value: Optional[float] = None
    baseline_value: Optional[float] = None
    delta_value: Optional[float] = None
    delta_ratio: Optional[float] = None

    model_config = ConfigDict()


class RunVerdictResponse(BaseModel):
    run_id: int
    verdict: str
    summary_text: str
    reason_codes: list[str] = Field(default_factory=list)
    baseline_run_id: Optional[int] = None
    baseline_scope_type: Optional[RunBaselineScopeType] = None
    baseline_scope_label: Optional[str] = None
    metric_deltas: list[RunVerdictMetricDelta] = Field(default_factory=list)

    model_config = ConfigDict()


class RunAIInsightItem(BaseModel):
    label: str
    detail: str

    model_config = ConfigDict()


class RunAIEvidenceItem(BaseModel):
    source: str
    label: str
    detail: str
    target_section: Optional[str] = None
    metric: Optional[str] = None
    severity: Optional[str] = None

    model_config = ConfigDict()


class RunAIRootCauseHypothesis(BaseModel):
    category: str
    hypothesis: str
    confidence: str
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    severity: Optional[str] = None
    metric: Optional[str] = None
    evidence_refs: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    model_config = ConfigDict()


class RunAIPrimaryFocus(BaseModel):
    kind: str
    label: str
    detail: Optional[str] = None
    url: Optional[str] = None
    dashboard_type: Optional[RunDashboardType] = None
    target_section: Optional[str] = None
    metric: Optional[str] = None

    model_config = ConfigDict()


class RunAIAnalystResponse(BaseModel):
    run_id: int
    verdict: str
    analyst_summary: str
    confidence: str
    input_sources: list[str] = Field(default_factory=list)
    primary_focus: Optional[RunAIPrimaryFocus] = None
    key_findings: list[RunAIInsightItem] = Field(default_factory=list)
    evidence_pack: list[RunAIEvidenceItem] = Field(default_factory=list)
    root_cause_hypotheses: list[RunAIRootCauseHypothesis] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    model_config = ConfigDict()


class RunAIReportFeedbackRating(str, enum.Enum):
    USEFUL = "useful"
    NOT_USEFUL = "not_useful"
    NEUTRAL = "neutral"


class RunAIReportFeedbackAction(str, enum.Enum):
    ACCEPTED = "accepted"
    NEEDS_RERUN = "needs_rerun"
    IGNORED = "ignored"


class RunAIReportFeedbackRequest(BaseModel):
    rating: RunAIReportFeedbackRating
    note: Optional[str] = Field(default=None, max_length=2000)
    action: Optional[RunAIReportFeedbackAction] = None


class RunAIReportResponse(BaseModel):
    report_id: int
    run_id: int
    status: str
    prompt_version: str
    profile: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    summary: Optional[str] = None
    key_findings: list[str] = Field(default_factory=list)
    anomalies: list[str] = Field(default_factory=list)
    root_cause_hypotheses: list[str] = Field(default_factory=list)
    root_cause_hypothesis_candidates: list[RunAIRootCauseHypothesis] = Field(
        default_factory=list
    )
    next_actions: list[str] = Field(default_factory=list)
    evidence_references: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    evidence_pack: list[RunAIEvidenceItem] = Field(default_factory=list)
    input_sources: list[str] = Field(default_factory=list)
    usage: dict[str, Optional[int]] = Field(default_factory=dict)
    latency_ms: Optional[int] = None
    error_message: Optional[str] = None
    disclaimer: str = "AI 生成，仅供辅助判断；结论必须结合 evidence pack 与人工复核。"
    created_by: Optional[int] = None
    feedback_rating: Optional[RunAIReportFeedbackRating] = None
    feedback_note: Optional[str] = None
    feedback_action: Optional[RunAIReportFeedbackAction] = None
    feedback_by: Optional[int] = None
    feedback_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class RunAIReportAcceptedResponse(BaseModel):
    run_id: int
    report_id: int
    accepted: bool = True
    async_task_id: str
    status: str = "pending"
    report: Optional[RunAIReportResponse] = None

    model_config = ConfigDict()


class RunAIReportTaskStatusResponse(BaseModel):
    run_id: int
    report_id: int
    async_task_id: str
    job_status: str
    completed: bool = False
    result: Optional[RunAIReportResponse] = None
    error: Optional[str] = None

    model_config = ConfigDict()


__all__ = [
    "RunCreate",
    "RunStopRequest",
    "RunK6ControlRequest",
    "RunBaselineSetRequest",
    "RunResponse",
    "RunAnalysisReadiness",
    "RunAnalysisReadinessSection",
    "MetricName",
    "MetricPoint",
    "MetricsSeries",
    "MetricsResponse",
    "LogItem",
    "LogsResponse",
    # Summary Metrics
    "RunSummaryMetricRow",
    "RunSummaryMetricsResponse",
    # Checks
    "RunCheckRow",
    "RunChecksResponse",
    # Pod Status
    "RunPodStatus",
    "RunPodStatusResponse",
    # Pod Monitor
    "RunPodMonitorMetric",
    "RunPodMonitorSeries",
    "RunPodMonitorSummary",
    "RunPodMonitorResponse",
    # Dashboards
    "RunDashboardType",
    "RunDashboardLink",
    "RunDashboardsResponse",
    # Endpoint Trend
    "EndpointTrendMetric",
    "EndpointTrendSeries",
    "EndpointTrendResponse",
    # Process
    "RunProcessStage",
    "RunProcessResponse",
    # K6 Control
    "RunK6ScenarioConfig",
    "RunK6ControlAgentState",
    "RunK6ControlSummary",
    "RunK6ControlResponse",
    "RunK6ControlAcceptedResponse",
    "RunK6ControlTaskStatusResponse",
    "RunCompareBundle",
    "RunCompareResponse",
    "RunBaselineRunSummary",
    "RunBaselineResponse",
    "RunVerdictMetricDelta",
    "RunVerdictResponse",
    "RunAIInsightItem",
    "RunAIEvidenceItem",
    "RunAIRootCauseHypothesis",
    "RunAIPrimaryFocus",
    "RunAIAnalystResponse",
    "RunAIReportFeedbackAction",
    "RunAIReportFeedbackRating",
    "RunAIReportFeedbackRequest",
    "RunAIReportAcceptedResponse",
    "RunAIReportTaskStatusResponse",
    "RunAIReportResponse",
]
