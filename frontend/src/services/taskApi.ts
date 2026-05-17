import request from '../utils/request'
import type { PaginationParams, PaginationResponse } from '../types'
import type { TaskAsset } from './taskAssetApi'

export type EngineType = 'jmeter' | 'k6' | 'custom'
export type TaskPattern = 'script' | 'visualization'
export type Protocol = 'http' | 'grpc' | 'kafka' | 'websocket' | 'browser' | 'other'
/**
 * 历史任务仍可能返回审批相关状态；主流程前端应将其视为兼容态，
 * 不再把审批语义当作正式前门状态展示。
 */
export type TaskStatus =
  | 'draft'
  | 'pending_submit_approval'
  | 'ready'
  | 'script_missing'
  | 'awaiting_approval'
  | 'approval_rejected'
  | 'running'

export interface Task {
  id: number
  name: string
  description?: string | null
  env: string
  engine_type: EngineType
  task_pattern: TaskPattern
  protocols: Protocol[]
  script_id: number
  thread_count: number
  duration: number
  ramp_up: number
  status: TaskStatus
  properties?: Record<string, unknown> | null
  script_name?: string | null
  created_at: string
  updated_at?: string | null
  created_by?: number | null
  created_by_name?: string | null
  collaborator_ids?: number[] | null
  collaborator_names?: string[] | null
  last_run_at?: string | null
  last_run_id?: number | null
  last_run_status?: string | null
  last_run_status_label?: string | null
  last_run_started_at?: string | null
  business_line?: string | null
  related_apps?: string[] | null
  monitor_link?: string | null
  trace_link?: string | null
  resource_type?: string | null
  cloud_vendor?: string | null
  pod_count?: number | null
  data_distribution?: string | null
  metrics_enabled?: boolean | null
  extra_dashboard_url?: string | null
  scenario_quality_lint?: ScenarioQualityLint | null
  proto_assets?: TaskAsset[]
  data_assets?: TaskAsset[]
}

export interface ScenarioQualityLintIssue {
  code: string
  label: string
  detail: string
  severity: 'warning' | string
  recommended_action?: string | null
}

export interface ScenarioQualityLint {
  status: 'clean' | 'warning' | string
  warning_count: number
  issues: ScenarioQualityLintIssue[]
}

export interface TaskLastRunParamsResponse {
  task_pattern: TaskPattern
  default_run_params?: Record<string, unknown> | null
  last_run_params?: Record<string, unknown> | null
  param_source?: 'default_only' | 'last_run_preferred' | null
  last_run_id?: number | null
  last_run_status?: string | null
  last_run_started_at?: string | null
  param_count?: number
}

export interface TaskPrepareRunResponse {
  task: Task
  next_action?: 'start_run' | 'submit_approval' | string | null
  status_hint?: string | null
}

export interface TaskBatchStopResponse {
  requested_task_ids: number[]
  stopped_task_ids: number[]
  stopped_run_ids: number[]
  skipped_task_ids: number[]
}

export interface TaskPodCapacityItem {
  cluster_code: string
  cluster_name: string
  available_pod_count: number
  resource_spec?: string | null
  readonly?: boolean
}

export interface TaskSummary {
  task_total: number
  task_success_total: number
  task_failed_total: number
  report_total: number
  pod_capacity: TaskPodCapacityItem[]
  resource_pool?: {
    standby_total: number
    in_use_total: number
    idle_total: number
    engine_compatibility: string[]
    standby_total_source?: string | null
    resource_spec?: string | null
  } | null
}

export interface TaskVersionRecord {
  id: number
  task_id: number
  version: string
  created_by_name?: string | null
  script_name?: string | null
  is_current?: boolean
  created_at: string
  updated_at?: string | null
}

export interface TaskVersionDetail {
  version: string
  created_by_name?: string | null
  created_at: string
  updated_at?: string | null
  snapshot: Record<string, unknown>
}

export interface TaskScriptVersionContent {
  version: string
  content: string
  file_name?: string | null
  created_by_name?: string | null
  updated_at?: string | null
}

export interface TaskScriptCompareResponse {
  current: TaskScriptVersionContent
  history: TaskScriptVersionContent
}

export interface TaskQuery extends PaginationParams {
  task_id?: number
  name?: string
  app_name?: string
  status?: TaskStatus
  last_run_status?: 'preparing' | 'running' | 'succeeded' | 'failed' | 'stopped'
  engine_type?: EngineType | string
  protocol?: Protocol | string
  env?: string
  created_by?: number
  created_by_name?: string
  participant_id?: number
  business_line?: string
  task_pattern?: TaskPattern | string
  from?: string
  to?: string
}

export interface CreateTaskDto {
  name: string
  description?: string | null
  env: string
  script_id: number
  engine_type: EngineType
  task_pattern?: TaskPattern
  protocols: Protocol[]
  thread_count: number
  duration: number
  ramp_up?: number
  properties?: Record<string, unknown> | null
  collaborator_ids?: number[] | null
}

export interface UpdateTaskDto extends Partial<CreateTaskDto> {}

export const taskApi = {
  // 获取任务列表
  getTaskList: (params: TaskQuery): Promise<PaginationResponse<Task>> => {
    return request.get('/tasks', { params })
  },

  // 获取任务详情
  getTaskDetail: (id: number): Promise<Task> => {
    return request.get(`/tasks/${id}`)
  },

  getTaskLastRunParams: (id: number): Promise<TaskLastRunParamsResponse> => {
    return request.get(`/tasks/${id}/last-run-params`)
  },

  getTaskSummary: (params: Partial<TaskQuery>): Promise<TaskSummary> => {
    return request.get('/tasks/summary', { params })
  },

  getTaskVersions: (
    id: number,
    params: { page?: number; pageSize?: number }
  ): Promise<PaginationResponse<TaskVersionRecord>> => {
    return request.get(`/tasks/${id}/versions`, { params })
  },

  getTaskVersionDetail: (
    id: number,
    version: string,
  ): Promise<TaskVersionDetail> => {
    return request.get(`/tasks/${id}/versions/${version}`)
  },

  compareTaskScript: (
    id: number,
    historyVersion: string
  ): Promise<TaskScriptCompareResponse> => {
    return request.get(`/tasks/${id}/script-compare`, {
      params: { history_version: historyVersion },
    })
  },

  // 创建任务
  createTask: (data: CreateTaskDto): Promise<Task> => {
    return request.post('/tasks', data)
  },

  // 更新任务
  updateTask: (id: number, data: UpdateTaskDto): Promise<Task> => {
    return request.put(`/tasks/${id}`, data)
  },

  prepareTaskForRun: (id: number): Promise<TaskPrepareRunResponse> => {
    return request.post(`/tasks/${id}/prepare-run`)
  },

  copyTask: (id: number): Promise<Task> => {
    return request.post(`/tasks/${id}/copy`)
  },

  batchStopTasks: (taskIds: number[], reason?: string): Promise<TaskBatchStopResponse> => {
    return request.post('/tasks/batch-stop', { task_ids: taskIds, reason })
  },

  // 删除任务
  deleteTask: (id: number): Promise<null> => {
    return request.delete(`/tasks/${id}`)
  },
}
