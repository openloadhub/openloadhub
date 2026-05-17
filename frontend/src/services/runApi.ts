import request from '../utils/request'
import type { PaginationParams, PaginationResponse } from '../types'

export type EngineType = 'jmeter' | 'k6' | 'custom'
export type RunStatus = 'preparing' | 'running' | 'succeeded' | 'failed' | 'stopped'
export type TaskPattern = 'script' | 'visualization'

export interface Run {
  run_id: number
  task_id: number
  task_name?: string | null
  engine_type: EngineType
  engine_type_label?: string | null
  protocol?: string | null
  env: string
  run_status: RunStatus
  run_status_label?: string | null
  run_status_detail?: string | null
  started_at?: string | null
  ended_at?: string | null
  duration_seconds?: number | null
  total_requests?: number | null
  success_rate?: number | null
  error_rate?: number | null
  avg_rt_ms?: number | null
  p95_rt_ms?: number | null
  p99_rt_ms?: number | null
  rps?: number | null
  stop_reason?: string | null
  // 业务线
  business_line?: string | null
  current_task_env?: string | null
  current_task_business_line?: string | null
  current_task_pattern?: TaskPattern | string | null
  current_task_protocols?: string[] | null
  // 执行节点数量 (总数/执行中/已完成)
  pod_total?: number | null
  pod_actual?: number | null
  pod_completed?: number | null
  // 操作人
  operator_name?: string | null
  run_window_label?: string | null
  agent_progress_label?: string | null
  overview_summary?: RunOverviewSummary | null
  analysis_readiness?: RunAnalysisReadiness | null
  params?: Record<string, unknown> | null
}

export interface RunOverviewSummary {
  total_requests?: number | null
  throughput?: number | null
  avg_rt_ms?: number | null
  p95_rt_ms?: number | null
  error_rate?: number | null
  checks_success_rate?: number | null
  endpoint_total?: number | null
  summary_metrics_label?: string | null
  checks_summary_label?: string | null
}

export interface RunAnalysisReadinessSection {
  status: 'ready' | 'partial' | 'missing' | string
  label: string
  detail?: string | null
  evidence: string[]
  gaps: string[]
}

export interface RunAnalysisReadiness {
  status: 'ready' | 'partial' | 'blocked' | string
  status_label: string
  evidence_ready: boolean
  required_sections: Record<string, RunAnalysisReadinessSection>
  evidence: string[]
  gaps: string[]
  limitations: string[]
  recommended_actions: string[]
}

export interface RunQuery extends PaginationParams {
  task_id?: number
  task_name?: string
  run_id?: number
  run_status?: RunStatus
  engine_type?: EngineType
  operator_name?: string
  protocol?: string
  business_line?: string
  task_pattern?: TaskPattern
  env?: string
  from?: string
  to?: string
}

export interface RunCreateRequest {
  task_id: number
  params?: Record<string, unknown> | null
}

export interface RunStopRequest {
  reason?: string | null
}

export type RunBaselineScopeType = 'task_env' | 'task_env_protocol'
export type RunBaselineSource = 'manual' | 'auto_latest_green'

export interface RunBaselineSetRequest {
  scope_type?: RunBaselineScopeType
  note?: string | null
  baseline_source?: RunBaselineSource
}

export interface RunBaselineRunSummary {
  run_id: number
  task_id: number
  task_name?: string | null
  env: string
  protocol?: string | null
  run_status: RunStatus
  started_at?: string | null
  ended_at?: string | null
}

export interface RunBaselineSummary {
  baseline_id: number
  scope_type: RunBaselineScopeType
  scope_key: string
  scope_label: string
  task_id: number
  env: string
  protocol?: string | null
  baseline_run_id: number
  baseline_source: RunBaselineSource
  effective_from?: string | null
  note?: string | null
  current_run_id?: number | null
  current_run_matches_baseline: boolean
  baseline_run?: RunBaselineRunSummary | null
}

export interface RunVerdictMetricDelta {
  metric: string
  unit?: string | null
  current_value?: number | null
  baseline_value?: number | null
  delta_value?: number | null
  delta_ratio?: number | null
}

export interface RunVerdictSummary {
  run_id: number
  verdict: 'pass' | 'warn' | 'fail'
  summary_text: string
  reason_codes: string[]
  baseline_run_id?: number | null
  baseline_scope_type?: RunBaselineScopeType | null
  baseline_scope_label?: string | null
  metric_deltas: RunVerdictMetricDelta[]
}

export interface RunAIInsightItem {
  label: string
  detail: string
}

export interface RunAIEvidenceItem {
  source: string
  label: string
  detail: string
  target_section?: 'monitor' | 'runtime_logs' | 'summary_metrics' | 'baseline' | string | null
  metric?: string | null
  severity?: 'pass' | 'warn' | 'fail' | string | null
}

export interface RunAIPrimaryFocus {
  kind: 'dashboard' | 'runtime_logs' | 'summary_metrics' | 'baseline' | string
  label: string
  detail?: string | null
  url?: string | null
  dashboard_type?: RunDashboardType | null
  target_section?: 'monitor' | 'runtime_logs' | 'summary_metrics' | 'baseline' | string | null
  metric?: string | null
}

export interface RunAIAnalystSummary {
  run_id: number
  verdict: 'pass' | 'warn' | 'fail'
  analyst_summary: string
  confidence: 'low' | 'medium' | 'high' | string
  input_sources: string[]
  primary_focus?: RunAIPrimaryFocus | null
  key_findings: RunAIInsightItem[]
  evidence_pack?: RunAIEvidenceItem[]
  recommended_actions: string[]
  limitations: string[]
}

export interface RunAIReportSummary {
  report_id: number
  run_id: number
  status: 'pending' | 'success' | 'failed' | string
  prompt_version: string
  provider?: string | null
  model?: string | null
  summary?: string | null
  key_findings: string[]
  anomalies: string[]
  root_cause_hypotheses: string[]
  next_actions: string[]
  evidence_references: string[]
  limitations: string[]
  evidence_pack?: RunAIEvidenceItem[]
  input_sources: string[]
  usage?: {
    prompt_tokens?: number | null
    completion_tokens?: number | null
    total_tokens?: number | null
  }
  latency_ms?: number | null
  error_message?: string | null
  disclaimer: string
  created_by?: number | null
  created_at?: string | null
  feedback_rating?: RunAIReportFeedbackRating | null
  feedback_note?: string | null
  feedback_action?: RunAIReportFeedbackAction | null
  feedback_by?: string | number | null
  feedback_at?: string | null
}

export interface RunAIReportAcceptedResponse {
  run_id: number
  report_id: number
  accepted: boolean
  async_task_id: string
  status: string
  report?: RunAIReportSummary | null
}

export interface RunAIReportTaskStatusResponse {
  run_id: number
  report_id: number
  async_task_id: string
  job_status: string
  completed: boolean
  result?: RunAIReportSummary | null
  error?: string | null
}

export type RunAIReportFeedbackRating = 'useful' | 'not_useful' | 'neutral'
export type RunAIReportFeedbackAction = 'accepted' | 'needs_rerun' | 'ignored'

export interface RunAIReportFeedbackRequest {
  rating: RunAIReportFeedbackRating
  note?: string
  action?: RunAIReportFeedbackAction
}

export type MetricName = 'rps' | 'rt_avg_ms' | 'rt_p95_ms' | 'rt_p99_ms' | 'error_rate'

export interface MetricPoint {
  ts: string
  value?: number | null
}

export interface MetricsSeries {
  metric: MetricName
  unit: string
  points: MetricPoint[]
}

export interface MetricsResponse {
  step_seconds: number
  series: MetricsSeries[]
}

export interface LogItem {
  seq: number
  ts: string
  level: string
  message: string
  source?: string | null
  raw_message?: string | null
  agent_host?: string | null
  agent_ip?: string | null
  source_kind?: 'tool' | 'platform' | 'unknown' | null
  channel?: 'stdout' | 'stderr' | 'event' | null
}

export interface LogsResponse {
  items: LogItem[]
  next_cursor?: string | null
}

// 接口级核心指标（每接口维度）
export interface InterfaceMetric {
  endpoint_name: string
  avg_rt_ms?: number | null
  p95_rt_ms?: number | null
  p99_rt_ms?: number | null
  max_rt_ms?: number | null
  min_rt_ms?: number | null
  total_requests?: number | null
  throughput?: number | null
}

// Group-Checks 聚合数据
export interface GroupCheck {
  group_name: string
  check_name: string
  success_rate: number
}

export interface SummaryMetricsResponse {
  items: InterfaceMetric[]
}

export interface ChecksResponse {
  items: GroupCheck[]
}

// 运行阶段信息
export interface RunProcessStage {
  name: string
  status: 'pending' | 'running' | 'completed' | 'failed'
  progress?: number | null
  started_at?: string | null
  ended_at?: string | null
  message?: string | null
}

export interface RunProcessResponse {
  run_status: RunStatus
  run_status_detail?: string | null
  stages: RunProcessStage[]
}

export interface RunK6ControlRequest {
  target_tps?: number | null
}

export interface RunK6ScenarioConfig {
  scenario_name: string
  executor?: string | null
  rate?: number | null
  time_unit?: string | null
  pre_allocated_vus?: number | null
  max_vus?: number | null
  vus?: number | null
  duration?: string | null
  exec_name?: string | null
}

export interface RunK6ControlAgentState {
  agent_host?: string | null
  available: boolean
  reason?: string | null
  supports_target_tps: boolean
  observed_tps?: number | null
  active_vus?: number | null
  scenario_pre_allocated_vus?: number | null
  scenario_max_vus?: number | null
  current_vus?: number | null
  current_max_vus?: number | null
  target_tps?: number | null
  controller_enabled: boolean
  controller_status?: string | null
  controller_message?: string | null
  metric_family?: string | null
  last_synced_at?: string | null
  control_strategy?: string | null
  preferred_control_path?: string | null
  active_control_path?: string | null
  scenario_patch_supported: boolean
  scenario_patch_reason?: string | null
  script_family?: string | null
  scenario_configs: RunK6ScenarioConfig[]
}

export interface RunK6ControlSummary {
  agent_total: number
  controllable_agent_total: number
  supports_target_tps: boolean
  observed_tps?: number | null
  active_vus?: number | null
  scenario_pre_allocated_vus?: number | null
  scenario_max_vus?: number | null
  current_vus?: number | null
  current_max_vus?: number | null
  target_tps?: number | null
  controller_enabled: boolean
  controller_status?: string | null
  controller_message?: string | null
  last_synced_at?: string | null
  control_strategy?: string | null
  preferred_control_path?: string | null
  active_control_path?: string | null
  scenario_patch_supported: boolean
  scenario_patch_reason?: string | null
  script_family?: string | null
}

export interface RunK6ControlResponse {
  run_id: number
  engine_type: 'k6' | string
  available: boolean
  reason?: string | null
  summary: RunK6ControlSummary
  agents: RunK6ControlAgentState[]
  warnings: string[]
}

export interface RunK6ControlAcceptedResponse {
  run_id: number
  accepted: boolean
  async_task_id: string
  target_tps?: number | null
}

export interface RunK6ControlTaskStatusResponse {
  run_id: number
  async_task_id: string
  job_status: string
  completed: boolean
  result?: RunK6ControlResponse | null
  error?: string | null
}

// Pod 状态
export interface RunPodStatus {
  agent_host?: string | null
  pod_ip?: string | null
  pod_name?: string | null
  status: string
  cluster_name?: string | null
  node_name?: string | null
  started_at?: string | null
  ended_at?: string | null
}

export interface RunPodsResponse {
  items: RunPodStatus[]
}

// Pod 监控指标点
export interface PodMonitorPoint {
  ts: string
  value?: number | null
}

// Pod 监控时序数据
export interface RunPodMonitorSeries {
  agent_host?: string | null
  pod_name?: string | null
  pod_ip?: string | null
  metric: string
  unit: string
  points: PodMonitorPoint[]
}

export interface RunPodMonitorSummary {
  observed_pod_total: number
  runtime_kind?: string | null
  resource_scope_label?: string | null
  cpu_usage_peak_percent?: number | null
  memory_usage_peak_percent?: number | null
  socket_peak?: number | null
  cpu_load_latest?: number | null
  network_rx_peak_bytes?: number | null
  network_tx_peak_bytes?: number | null
  cpu_summary_label?: string | null
  memory_summary_label?: string | null
  network_summary_label?: string | null
  runtime_summary_label?: string | null
}

export interface RunPodMonitorResponse {
  step_seconds: number
  series: RunPodMonitorSeries[]
  summary: RunPodMonitorSummary
}

// Dashboard 类型
export type RunDashboardType = 'related_monitor' | 'pod_grafana' | 'engine_grafana' | 'topology' | 'server_config'

// Dashboard 链接
export interface RunDashboardLink {
  dashboard_type: RunDashboardType
  title: string
  url: string
  embed_mode?: 'iframe' | 'new_tab'
}

export interface RunDashboardSummary {
  has_engine_grafana: boolean
  has_pod_grafana: boolean
  related_monitor_total: number
  topology_total: number
  server_config_total: number
  total_dashboard_count: number
  engine_grafana_label?: string | null
  pod_grafana_label?: string | null
  related_monitor_label?: string | null
  topology_label?: string | null
  server_config_label?: string | null
}

export interface RunDashboardsResponse {
  items: RunDashboardLink[]
  summary: RunDashboardSummary
}

export type RunAlertEventStatus = 'firing' | 'resolved' | string
export type RunAlertEventSeverity = 'critical' | 'high' | 'warning' | 'info' | string

export interface RunAlertEvent {
  id?: number | string | null
  event_id?: number | string | null
  run_id?: number | null
  task_id?: number | null
  mixed_run_id?: number | null
  plan_run_id?: number | null
  status?: RunAlertEventStatus | null
  severity?: RunAlertEventSeverity | null
  priority?: string | null
  alertname?: string | null
  subscription?: string | null
  source?: string | null
  starts_at?: string | null
  ends_at?: string | null
  labels?: Record<string, unknown> | null
  annotations?: Record<string, unknown> | null
  dashboard_url?: string | null
  action_status?: string | null
}

export interface RunAlertEventsSummary {
  total: number
  firing_total: number
  resolved_total: number
  highest_severity?: RunAlertEventSeverity | null
}

export interface RunAlertEventsResponse {
  items: RunAlertEvent[]
  summary: RunAlertEventsSummary
}

export type EndpointTrendMetric = 'rt_avg_ms' | 'rt_p95_ms' | 'rt_p99_ms' | 'throughput' | 'error_rate'

export interface EndpointTrendSeries {
  endpoint_name: string
  metric: EndpointTrendMetric
  unit: string
  points: MetricPoint[]
}

export interface EndpointTrendResponse {
  step_seconds: number
  items: EndpointTrendSeries[]
}

export interface RunCompareBundle {
  detail: Run
  overview_summary: RunOverviewSummary
  baseline?: RunBaselineSummary | null
  summary: SummaryMetricsResponse
  checks: ChecksResponse
  metrics: MetricsResponse
  dashboards: RunDashboardsResponse
  endpoint_trends: Partial<Record<EndpointTrendMetric, EndpointTrendResponse>>
}

export interface RunCompareResponse {
  base_id: number
  comparator_id: number
  base: RunCompareBundle
  comparator: RunCompareBundle
}

export const runApi = {
  getRunList: (params: RunQuery): Promise<PaginationResponse<Run>> => {
    return request.get('/runs', { params })
  },
  getRunDetail: (runId: number): Promise<Run> => {
    return request.get(`/runs/${runId}`)
  },
  getRunCompare: (baseId: number, comparatorId: number): Promise<RunCompareResponse> => {
    return request.get('/runs/compare', { params: { base_id: baseId, comparator_id: comparatorId } })
  },
  getRunBaseline: (runId: number, params?: { scope_type?: RunBaselineScopeType }): Promise<RunBaselineSummary | null> => {
    return request.get(`/runs/${runId}/baseline`, { params })
  },
  setRunBaseline: (runId: number, data: RunBaselineSetRequest): Promise<RunBaselineSummary> => {
    return request.post(`/runs/${runId}/baseline`, data)
  },
  getRunVerdict: (runId: number): Promise<RunVerdictSummary> => {
    return request.get(`/runs/${runId}/verdict`)
  },
  getRunAIAnalyst: (runId: number): Promise<RunAIAnalystSummary> => {
    void runId
    return Promise.reject(new Error('Run AI analyst is not included in public alpha'))
  },
  getLatestRunAIReport: (runId: number): Promise<RunAIReportSummary> => {
    void runId
    return Promise.reject(new Error('Run AI report is not included in public alpha'))
  },
  generateRunAIReport: (runId: number): Promise<RunAIReportSummary> => {
    void runId
    return Promise.reject(new Error('Run AI report is not included in public alpha'))
  },
  submitAsyncRunAIReport: (runId: number): Promise<RunAIReportAcceptedResponse> => {
    void runId
    return Promise.reject(new Error('Run AI report is not included in public alpha'))
  },
  getAsyncRunAIReportTask: (
    runId: number,
    taskId: string,
    reportId: number,
  ): Promise<RunAIReportTaskStatusResponse> => {
    void runId
    void taskId
    void reportId
    return Promise.reject(new Error('Run AI report is not included in public alpha'))
  },
  submitRunAIReportFeedback: (
    runId: number,
    reportId: number,
    data: RunAIReportFeedbackRequest,
  ): Promise<RunAIReportSummary> => {
    void runId
    void reportId
    void data
    return Promise.reject(new Error('Run AI report is not included in public alpha'))
  },
  createRun: (data: RunCreateRequest, idempotencyKey?: string): Promise<Run> => {
    return request.post('/runs', data, {
      headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined,
    })
  },
  stopRun: (runId: number, data?: RunStopRequest): Promise<Run> => {
    return request.post(`/runs/${runId}/stop`, data ?? {})
  },
  getRunMetrics: (
    runId: number,
    params?: { from?: string; to?: string; metric?: MetricName; step_seconds?: number }
  ): Promise<MetricsResponse> => {
    return request.get(`/runs/${runId}/metrics`, { params })
  },
  getRunEndpointTrends: (
    runId: number,
    params?: { metric?: EndpointTrendMetric; endpoint_name?: string; step_seconds?: number }
  ): Promise<EndpointTrendResponse> => {
    return request.get(`/runs/${runId}/endpoint-trends`, { params })
  },
  getRunLogs: (
    runId: number,
    params?: {
      cursor?: string
      limit?: number
      view?: 'all' | 'exception'
      level?: string
      source?: string
      order?: 'asc' | 'desc'
    }
  ): Promise<LogsResponse> => {
    return request.get(`/runs/${runId}/logs`, { params })
  },
  // 接口级核心指标（详情页接口维度表）
  getSummaryMetrics: (runId: number): Promise<SummaryMetricsResponse> => {
    return request.get(`/runs/${runId}/summary-metrics`, { params: {} })
  },
  // Group-Checks 聚合
  getChecks: (runId: number): Promise<ChecksResponse> => {
    return request.get(`/runs/${runId}/checks`, { params: {} })
  },
  // 运行过程（阶段流转）
  getRunProcess: (runId: number): Promise<RunProcessResponse> => {
    return request.get(`/runs/${runId}/process`)
  },
  getRunK6Control: (runId: number): Promise<RunK6ControlResponse> => {
    void runId
    return Promise.reject(new Error('Dynamic K6 control is roadmap-only in public alpha'))
  },
  updateRunK6Control: (
    runId: number,
    data: RunK6ControlRequest,
  ): Promise<RunK6ControlResponse> => {
    void runId
    void data
    return Promise.reject(new Error('Dynamic K6 control is roadmap-only in public alpha'))
  },
  submitAsyncRunK6Control: (
    runId: number,
    data: RunK6ControlRequest,
  ): Promise<RunK6ControlAcceptedResponse> => {
    void runId
    void data
    return Promise.reject(new Error('Dynamic K6 control is roadmap-only in public alpha'))
  },
  getAsyncRunK6ControlTask: (
    runId: number,
    taskId: string,
  ): Promise<RunK6ControlTaskStatusResponse> => {
    void runId
    void taskId
    return Promise.reject(new Error('Dynamic K6 control is roadmap-only in public alpha'))
  },
  // Pod 状态列表
  getRunPods: (runId: number): Promise<RunPodsResponse> => {
    return request.get(`/runs/${runId}/pods`)
  },
  // Pod 资源监控数据
  getRunPodsMonitor: (
    runId: number,
    params?: { step_seconds?: number }
  ): Promise<RunPodMonitorResponse> => {
    return request.get(`/runs/${runId}/pods/monitor`, { params })
  },
  // Dashboard 入口列表
  getRunDashboards: (runId: number): Promise<RunDashboardsResponse> => {
    return request.get(`/runs/${runId}/dashboards`)
  },
  // 外部告警事件
  getRunAlertEvents: (runId: number): Promise<RunAlertEventsResponse> => {
    return request.get(`/runs/${runId}/alert-events`, { skipErrorHandler: true })
  },
}
