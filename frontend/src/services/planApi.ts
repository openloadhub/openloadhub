import request from '../utils/request'
import type { PaginationParams, PaginationResponse } from '../types'
import type { ScenarioQualityLint } from './taskApi'

export type PlanStatus = 'ready' | 'suspended' | 'running' | 'deleted'
export type PlanExecType = 'manual' | 'cron' | 'fixed'
export type PlanRunStatus =
  | 'preparing'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'stopped'
  | 'suspended'
  | 'duplicated'

export type PlanStageItemType = 'task' | 'postprocessor'
export type TaskPattern = string

export interface PlanStageItemRoundOverride {
  round: number
  run_params?: Record<string, any> | null
  post_params?: Record<string, any> | null
}

export interface PlanStageItem {
  type: PlanStageItemType
  task_id?: number | null
  task_name?: string | null
  task_pattern?: TaskPattern | null
  run_params?: Record<string, any> | null
  post_params?: Record<string, any> | null
  round_overrides?: PlanStageItemRoundOverride[] | null
  retry_count: number
  retry_delay_seconds: number
  on_failure: 'ABORT' | 'SKIP' | 'CONTINUE'
}

export interface PlanStage {
  stage: number
  items: PlanStageItem[]
}

export interface PlanExecutionEstimate {
  round_count: number
  single_round_seconds: number
  total_seconds: number
  task_seconds: number
  interval_seconds: number
  stage_seconds: number[]
  deterministic: boolean
  start_at?: string | null
  end_at?: string | null
}

export interface Plan {
  plan_id: number
  name: string
  description?: string | null
  status: PlanStatus
  status_label?: string | null
  exec_type: PlanExecType
  exec_type_label?: string | null
  cron?: string | null
  scheduled_at?: string | null
  timezone?: string | null
  enable_round?: boolean
  total_round?: number | null
  round_mode_label?: string | null
  stages: PlanStage[]
  stage_total?: number | null
  task_total?: number | null
  postprocessor_total?: number | null
  env_list?: string[] | null
  business_lines?: string[] | null
  engine_types?: string[] | null
  collaborator_ids?: number[] | null
  collaborator_names?: string[] | null
  secondary_summary?: string | null
  execution_estimate?: PlanExecutionEstimate | null
  scenario_quality_lint?: ScenarioQualityLint | null
  latest_plan_run_id?: number | null
  latest_plan_run_status?: PlanRunStatus | null
  latest_plan_run_status_label?: string | null
  latest_plan_run_started_at?: string | null
  latest_plan_run_ended_at?: string | null
  created_at: string
  updated_at?: string | null
  created_by?: number | null
  created_by_name?: string | null
}

export interface PlanCopyResponse extends Plan {}

export interface PlanQuery extends PaginationParams {
  plan_id?: number
  name?: string
  status?: PlanStatus
  exec_type?: PlanExecType
  created_by?: number
  created_by_name?: string
  participant_id?: number
  business_line?: string
  env?: string
  engine_type?: string
  round_mode?: 'single' | 'multi'
  time_field?: 'created_at' | 'updated_at'
  from?: string
  to?: string
}

export interface PlanCreateRequest extends Omit<Plan, 'plan_id' | 'created_at' | 'updated_at' | 'created_by'> {}
export interface PlanUpdateRequest extends Partial<PlanCreateRequest> {}

export interface PlanExecuteResponse {
  plan_run_id: number
  status: PlanRunStatus
  start_type?: number | null
}

export interface PlanRunReportItem {
  collection_id?: number | null
  run_id: number
  task_name?: string | null
  resolution_status: 'ready' | 'template_fallback' | 'missing' | string
  message: string
  report_id?: number | null
  report_name?: string | null
}

export interface PlanRunReportSummary {
  linked_run_total: number
  ready_total: number
  template_fallback_total: number
  missing_total: number
  conclusion_status: 'ready' | 'partial' | 'missing' | string
  conclusion_text: string
  selected_run_status?: 'ready' | 'template_fallback' | 'missing' | string | null
  selected_run_report_id?: number | null
  selected_run_report_name?: string | null
  selected_run_message?: string | null
  items: PlanRunReportItem[]
}

export interface PlanRunAdvisoryFinding {
  label: string
  detail: string
}

export interface PlanRunAdvisoryPrimaryFocus {
  kind: 'failed_run' | 'failed_postprocessor' | 'ready_report' | 'missing_report' | 'template_fallback_report' | string
  label: string
  detail?: string | null
  collection_id?: number | null
  run_id?: number | null
  report_id?: number | null
}

export interface PlanRunAdvisorySummary {
  verdict: 'pass' | 'warn' | 'fail' | string
  delivery_status?: 'ready_for_review' | 'needs_attention' | 'blocked' | string
  delivery_status_label?: string | null
  evidence_ready?: boolean
  summary_text: string
  input_sources: string[]
  primary_focus?: PlanRunAdvisoryPrimaryFocus | null
  key_findings: PlanRunAdvisoryFinding[]
  evidence_gaps?: string[]
  blocking_reasons?: string[]
  recommended_actions: string[]
  limitations: string[]
}

export interface PlanRun {
  plan_run_id: number
  plan_id: number
  plan_name?: string | null
  plan_exec_type?: PlanExecType | null
  plan_exec_type_label?: string | null
  status: PlanRunStatus
  status_label?: string | null
  status_detail?: string | null
  launched_run_ids?: number[] | null
  stages_snapshot?: PlanStage[] | null
  started_at?: string | null
  ended_at?: string | null
  duration_seconds?: number | null
  round?: number | null
  round_total?: number | null
  round_label?: string | null
  round_mode_label?: string | null
  stage_total?: number | null
  task_total?: number | null
  postprocessor_total?: number | null
  launched_run_total?: number | null
  planned_total_seconds?: number | null
  planned_task_seconds?: number | null
  planned_interval_seconds?: number | null
  env_list?: string[] | null
  business_lines?: string[] | null
  engine_types?: string[] | null
  collaborator_names?: string[] | null
  summary_label?: string | null
  report_ready_total?: number | null
  report_template_fallback_total?: number | null
  report_missing_total?: number | null
  advisory_verdict?: 'pass' | 'warn' | 'fail' | string | null
  can_stop?: boolean
  latest_failed_run_id?: number | null
  latest_failed_task_name?: string | null
  rounds?: Array<{
    round: number
    label?: string | null
    status?: PlanRunStatus | null
    status_label?: string | null
  }> | null
  task_exec_records?: Array<{
    collection_id?: number | null
    stage: number
    logic_type: 'task' | 'postprocessor' | string
    task_id?: number | null
    task_name?: string | null
    run_id?: number | null
    status?: string | null
    status_label?: string | null
    logic_type_label?: string | null
    started_at?: string | null
    ended_at?: string | null
    run_params?: Record<string, unknown> | null
    env?: string | null
    engine_type?: string | null
    total_requests?: number | null
    rps?: number | null
    error_rate?: number | null
    avg_rt_ms?: number | null
    p95_rt_ms?: number | null
    p99_rt_ms?: number | null
    success_rate?: number | null
    metric_summary?: string | null
    status_reason?: string | null
    postprocessor_kind?: string | null
    postprocessor_seconds?: number | null
  }> | null
  selected_round?: number | null
  execution_summary?: {
    total_records: number
    task_total: number
    postprocessor_total: number
    succeeded_total: number
    failed_total: number
    stopped_total: number
    pending_total: number
    launched_run_total: number
    total_throughput?: number | null
    weighted_error_rate?: number | null
    max_avg_rt_ms?: number | null
    max_p95_rt_ms?: number | null
    max_p99_rt_ms?: number | null
    peak_qps?: number | null
  } | null
  selected_round_summary?: {
    round: number
    label?: string | null
    status?: PlanRunStatus | null
    status_label?: string | null
    summary_label?: string | null
    total_records: number
    task_total: number
    postprocessor_total: number
    succeeded_total: number
    failed_total: number
    stopped_total: number
    pending_total: number
    launched_run_total: number
    started_at?: string | null
    ended_at?: string | null
  } | null
  selected_collection_id?: number | null
  selected_task_summary?: {
    collection_id?: number | null
    record_index?: number | null
    record_total?: number | null
    position_label?: string | null
    stage: number
    logic_type: string
    logic_type_label?: string | null
    task_id?: number | null
    task_name?: string | null
    run_id?: number | null
    status?: string | null
    status_label?: string | null
    started_at?: string | null
    ended_at?: string | null
    duration_seconds?: number | null
    run_params?: Record<string, unknown> | null
    env?: string | null
    engine_type?: string | null
    total_requests?: number | null
    rps?: number | null
    error_rate?: number | null
    avg_rt_ms?: number | null
    p95_rt_ms?: number | null
    p99_rt_ms?: number | null
    success_rate?: number | null
    metric_summary?: string | null
    status_reason?: string | null
    postprocessor_kind?: string | null
    postprocessor_seconds?: number | null
  } | null
  selected_interface_summary?: {
    collection_id?: number | null
    run_id?: number | null
    task_name?: string | null
    source: 'seed' | 'agent' | 'archive' | 'none' | string
    metric_total: number
    total_requests?: number | null
    total_throughput?: number | null
    max_avg_rt_ms?: number | null
    max_p95_rt_ms?: number | null
    max_p99_rt_ms?: number | null
    endpoint_preview: string[]
    empty_reason?: string | null
  } | null
  selected_run_summary?: {
    run_id: number
    task_name?: string | null
    status?: string | null
    status_label?: string | null
    env?: string | null
    engine_type?: string | null
    duration_seconds?: number | null
    total_requests?: number | null
    rps?: number | null
    error_rate?: number | null
    avg_rt_ms?: number | null
    p95_rt_ms?: number | null
    p99_rt_ms?: number | null
    success_rate?: number | null
    started_at?: string | null
    ended_at?: string | null
    metric_summary?: string | null
  } | null
  report_summary?: PlanRunReportSummary | null
  advisory_summary?: PlanRunAdvisorySummary | null
  selected_round_status?: PlanRunStatus | null
  collaborator_ids?: number[] | null
  created_at: string
  created_by?: number | null
  created_by_name?: string | null
}

export interface PlanRunQuery extends PaginationParams {
  plan_id?: number
  plan_run_id?: number
  plan_name?: string
  business_line?: string
  env?: string
  engine_type?: string
  round_mode?: 'single' | 'multi'
  status?: PlanRunStatus
  created_by?: number
  created_by_name?: string
  from?: string
  to?: string
}

export interface PlanRunK6BroadcastRequest {
  selected_round?: number | null
  ratio?: number | null
  target_total_tps?: number | null
  target_total_max_vus?: number | null
  async_mode?: boolean
}

export interface PlanRunK6BroadcastItem {
  collection_id?: number | null
  run_id: number
  task_name?: string | null
  env?: string | null
  base_target_tps?: number | null
  target_tps?: number | null
  status?: string | null
  success: boolean
  detail?: string | null
}

export interface PlanRunK6BroadcastResponse {
  plan_run_id: number
  selected_round: number
  controllable_run_total: number
  success_run_total: number
  failed_run_total: number
  skipped_run_total: number
  ratio: number
  base_total_tps?: number | null
  target_total_tps?: number | null
  items: PlanRunK6BroadcastItem[]
  warnings: string[]
}

export interface PlanRunK6BroadcastAcceptedResponse {
  plan_run_id: number
  selected_round: number
  accepted: boolean
  async_task_id: string
  ratio: number
  base_total_tps?: number | null
  target_total_tps?: number | null
}

export interface PlanRunK6BroadcastTaskStatusResponse {
  plan_run_id: number
  selected_round: number
  async_task_id: string
  job_status: string
  completed: boolean
  result?: PlanRunK6BroadcastResponse | null
  error?: string | null
}

export const planApi = {
  getPlanList: (params: PlanQuery): Promise<PaginationResponse<Plan>> => {
    return request.get('/plans', { params })
  },
  getPlanDetail: (planId: number): Promise<Plan> => {
    return request.get(`/plans/${planId}`)
  },
  createPlan: (data: PlanCreateRequest): Promise<Plan> => {
    return request.post('/plans', data)
  },
  updatePlan: (planId: number, data: PlanUpdateRequest): Promise<Plan> => {
    return request.put(`/plans/${planId}`, data)
  },
  deletePlan: (planId: number): Promise<null> => {
    return request.delete(`/plans/${planId}`)
  },
  copyPlan: (planId: number): Promise<PlanCopyResponse> => {
    return request.post(`/plans/${planId}/copy`)
  },
  executePlan: (planId: number, data?: { start_type?: number }): Promise<PlanExecuteResponse> => {
    return request.post(`/plans/${planId}/execute`, data ?? {})
  },
  getPlanRunList: (params: PlanRunQuery): Promise<PaginationResponse<PlanRun>> => {
    return request.get('/plan-runs', { params })
  },
  getPlanRunDetail: (planRunId: number, params?: { round?: number; collection_id?: number }): Promise<PlanRun> => {
    return request.get(`/plan-runs/${planRunId}`, { params })
  },
  downloadPlanRunReportManifest: (
    planRunId: number,
    params?: { round?: number; collection_id?: number },
  ): Promise<Blob> => {
    return request.get(`/plan-runs/${planRunId}/report-manifest`, {
      params,
      responseType: 'blob',
    })
  },
  broadcastPlanRunK6Control: (
    planRunId: number,
    data: PlanRunK6BroadcastRequest,
  ): Promise<PlanRunK6BroadcastResponse> => {
    void planRunId
    void data
    return Promise.reject(new Error('Dynamic K6 control is roadmap-only in public alpha'))
  },
  submitAsyncPlanRunK6Control: (
    planRunId: number,
    data: PlanRunK6BroadcastRequest,
  ): Promise<PlanRunK6BroadcastAcceptedResponse> => {
    void planRunId
    void data
    return Promise.reject(new Error('Dynamic K6 control is roadmap-only in public alpha'))
  },
  getAsyncPlanRunK6ControlTask: (
    planRunId: number,
    taskId: string,
  ): Promise<PlanRunK6BroadcastTaskStatusResponse> => {
    void planRunId
    void taskId
    return Promise.reject(new Error('Dynamic K6 control is roadmap-only in public alpha'))
  },
  stopPlanRun: (planRunId: number): Promise<PlanRun> => {
    return request.post(`/plan-runs/${planRunId}/stop`)
  },
}
